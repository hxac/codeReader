# NPUModelRunner v2 新架构

## 1. 本讲目标

本讲聚焦 vllm-ascend 在每张 NPU 卡上「跑前向」的第二代实现——`vllm_ascend/worker/v2/` 目录下的 **NPUModelRunner v2**（也称 MRV2）。读完后你应当能够：

- 说清楚 v2 与 v1 在「状态管理」上的根本差异，以及为什么 vllm-ascend 要紧跟上游 v2。
- 掌握 v2 的状态三件套 `AscendRequestState` / `AscendInputBatch` / `AscendInputBuffers` 各自的职责，以及它们围绕 `seq_lens_cpu` 展开的「补回」逻辑，包括最近一次优化——非投机解码时跳过 D2H 拷贝与同步（#13382）。
- 理解 `AscendModelState` 如何接管注意力元数据构建，以及 PCP（Prefill Context Parallel）管理器在 v2 里的作用。
- 理解 v2 为何要重写/修复若干 Triton 算子（#13159 中 `get_num_nans` 的 libdevice 解析、`apply_penalties` 的过网格）。
- 能够对照源码，把「v2 启用开关 → worker 选 runner → runner 初始化 → 执行主链路」这条链路讲给别人听。

> 提示：v2 目前仍是**实验性**架构（目录带 `[Experimental]` 标记），部分特性尚未对齐。本讲会明确指出哪些地方「待本地验证」或「开发中」。

## 2. 前置知识

在学习本讲前，你需要先建立以下概念（它们来自前置讲义 u4-l1、u4-l2）：

- **NPUWorker 与 ModelRunner 的分工**：每个 worker 子进程持有一个 `NPUWorker`，它负责单卡资源与生命周期编排，真正的前向计算委托给 `ModelRunner`。
- **v1 的执行主链路**：`_update_states → _prepare_inputs → _build_attention_metadata → _model_forward → _sample`。
- **`torch_cuda_wrapper`**：因为上游 vLLM 的 GPU 路径大量调用 `torch.cuda.*`，vllm-ascend 用一个上下文管理器把 `torch.cuda.Event/Stream/...` 临时指向 `torch.npu.*`，从而让父类初始化代码在 NPU 上也能跑通（见 [utils.py:L10-L27](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/v2/utils.py#L10-L27)）。
- **`AscendAttentionState` 状态机**：如 `PrefillNoCache` / `DecodeOnly` / `ChunkedPrefill` / `SpecDecoding` 等，NPU 注意力后端用它来选择不同的内核分支。

还需要理解一个上游背景：vLLM 社区本身正在把 `GPUModelRunner` 重构为 v2，把原本堆在一个巨型类里的状态拆成若干**模块化子系统**（`model_states/`、`input_batch`、`states`）。vllm-ascend 的 v2 就是「继承上游 v2 + 做最小 Ascend 定制」，而不再像 v1 那样在巨型类里大面积重写。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [vllm_ascend/worker/v2/model_runner.py](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/v2/model_runner.py) | v2 的主角 `NPUModelRunner`，继承上游 `GPUModelRunner`，做最小改写。 |
| [vllm_ascend/worker/v2/states.py](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/v2/states.py) | `AscendRequestState`，按请求维度管理状态，额外保留 `num_computed_tokens_cpu`。 |
| [vllm_ascend/worker/v2/input_batch.py](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/v2/input_batch.py) | `AscendInputBuffers` 与 `AscendInputBatch`，承载单次前向的输入与 NPU 专属的 `seq_lens_cpu/seq_lens_np`。 |
| [vllm_ascend/worker/v2/model_states/default.py](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/v2/model_states/default.py) | `AscendModelState`，接管注意力元数据 `prepare_attn` 的构建。 |
| [vllm_ascend/worker/v2/model_states/__init__.py](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/v2/model_states/__init__.py) | 工厂函数 `init_asecnd_model_state`（原文如此拼写），供上游调用以创建 Ascend 版 ModelState。 |
| [vllm_ascend/worker/v2/pcp_manager.py](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/v2/pcp_manager.py) | `AscendPCPManager`，把 Prefill Context Parallel 的批次切分后刷新 Ascend 专属元数据。 |
| [vllm_ascend/worker/v2/attn_utils.py](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/v2/attn_utils.py) | `build_attn_metadata` / `build_attn_state` 等 NPU 注意力工具函数。 |
| [vllm_ascend/worker/v2/sample/penalties.py](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/v2/sample/penalties.py) | v2 重写的采样惩罚 Triton 算子（`apply_penalties` / `bincount`），#13159 修复了过网格问题。 |
| [vllm_ascend/patch/worker/patch_v2/patch_triton.py](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/patch/worker/patch_v2/patch_triton.py) | v2 的 Triton 补丁总入口：把上游 sample/spec 算子重绑到 Ascend 版，并修复 `get_num_nans` 的 libdevice 解析。 |
| [vllm_ascend/patch/platform/patch_use_v2_model_runner.py](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/patch/platform/patch_use_v2_model_runner.py) | platform 补丁，把「是否启用 v2」的决定权交给环境变量。 |
| [vllm_ascend/worker/v2/README.md](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/v2/README.md) | 官方「与 vLLM 的差距清单」，是理解 v2 设计取舍的一手资料。 |

## 4. 核心概念与源码讲解

本讲拆成 6 个最小模块：

1. v2 架构总览与启用机制
2. 状态三件套（states / input_batch / buffers）与 seq_lens_cpu 异步通路
3. 注意力元数据构建与 AscendModelState
4. PCP 上下文并行管理器
5. 执行主链路与 ACL Graph 接管
6. v2 特有的 Triton 兼容修复（num_nans / penalties）

### 4.1 v2 架构总览与启用机制

#### 4.1.1 概念说明

vLLM 社区把 `GPUModelRunner` 重构为 v2，核心理念是**「把状态从 runner 类里剥离成独立子系统」**：

- `model_states/`：负责模型级状态（KV cache、attention metadata 构建、block table 等）。
- `input_batch.py`：负责「这一次前向的输入批次」的数据结构。
- `states.py`：负责「按请求维度」的运行期状态（每个请求算到第几个 token 等）。

vllm-ascend 的 v2 策略是**紧跟上游、最小侵入**：直接继承上游 v2 的 `GPUModelRunner`，让父类干大部分活，只在「NPU 与 GPU 行为不一致」处替换。这与 v1「在巨型类里大面积重写 `_prepare_inputs` / `execute_model` / `_build_attention_metadata`」的风格形成鲜明对比。

> 一句话区别：**v1 = 重写为主；v2 = 复用上游为主 + 三件套替换**。

v2 当前仍是实验性架构，目录 README 明确标注 `[Experimental]`，并在 [v2/README.md:L8-L39](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/v2/README.md#L8-L39) 列出了若干「待消除的差距（Gaps with vLLM）」，例如 `set_cos_and_sin`、自定义 KV cache 分配、`torch_npu_graph_wrapper` 等。

#### 4.1.2 核心流程

v2 的启用与选中链路如下：

```text
进程启动
  └─ VllmConfig.use_v2_model_runner   ← 被 platform 补丁替换为「只读环境变量」
        └─ NPUWorker.__init__ 读取 self.use_v2_model_runner
              ├─ True  → import v2.NPUModelRunner，构造 NPUModelRunnerV2
              └─ False → 用 v1 的 NPUModelRunner（默认）
```

关键点：上游 `use_v2_model_runner` 本身带有「按模型架构白名单 + Triton 可用性 + 特性探测」的一系列把关；而 vllm-ascend 用一个 platform 补丁把它**简化**为「只看环境变量 `VLLM_USE_V2_MODEL_RUNNER`」，把模型兼容性判断推迟到 runner 自身。

#### 4.1.3 源码精读

先看启用开关的补丁。[patch_use_v2_model_runner.py](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/patch/platform/patch_use_v2_model_runner.py) 把 `VllmConfig.use_v2_model_runner` 替换为一个 `property`，直接返回环境变量：

```python
def _patched_use_v2_model_runner(self) -> bool:
    """Return VLLM_USE_V2_MODEL_RUNNER env directly.
    ... model-compatibility decisions are deferred to the NPU runner itself.
    """
    use_v2 = envs.VLLM_USE_V2_MODEL_RUNNER
    if use_v2 is not None:
        return use_v2
    return False

VllmConfig.use_v2_model_runner = property(_patched_use_v2_model_runner)
```

然后在 worker 里据此分流，见 [worker.py:L493-L496](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/worker.py#L493-L496)：

```python
if self.use_v2_model_runner:
    logger.warning("npu model runner v2 is in developing, some features doesn't work for now.")
    from vllm_ascend.worker.v2.model_runner import NPUModelRunner as NPUModelRunnerV2
    self.model_runner = NPUModelRunnerV2(self.vllm_config, self.device)
else:
    self.model_runner = NPUModelRunner(self.vllm_config, self.device)
```

注意那句 `logger.warning(...)`——它明确告诉用户 v2 还在开发中。这也是本讲多处「待本地验证」的原因。

#### 4.1.4 代码实践

**实践目标**：确认本机 / 本仓库里 v2 的启用方式。

**操作步骤**：

1. 打开 [patch_use_v2_model_runner.py](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/patch/platform/patch_use_v2_model_runner.py)，确认它读取的环境变量名。
2. 在仓库根目录搜索 `VLLM_USE_V2_MODEL_RUNNER`，看它是否出现在文档、示例或 CI 配置里（说明官方在哪些场景推荐 v2）。

**需要观察的现象**：环境变量未设置时 `use_v2_model_runner` 返回 `False`，worker 走 v1（默认稳态）；设为 `1`/`true` 才会进入 v2 分支。

**预期结果**：v2 是**默认关闭**的，需要显式开启。

#### 4.1.5 小练习与答案

**练习 1**：为什么 vllm-ascend 要把上游的 `use_v2_model_runner` 白名单逻辑去掉，改成只看环境变量？

> **参考答案**：上游的白名单是针对 GPU/Triton 生态的兼容性探测，在 Ascend 上并不适用；vllm-ascend 希望把「能不能用 v2」的判断权留给自己（runner 内部根据 NPU 能力决定，例如下面会看到 `dynamic_eplb` 不支持就直接抛错），而不是被上游的 GPU 假设误判。

**练习 2**：如果不打这个补丁，直接设 `VLLM_USE_V2_MODEL_RUNNER=1` 会发生什么？

> **参考答案**：上游的 `use_v2_model_runner` 可能因为模型不在白名单或 Triton 探测失败而返回 `False`，导致即便设了环境变量也进不了 v2 分支。补丁的作用就是消除这层「额外把关」，让环境变量成为唯一开关。

---

### 4.2 状态三件套：states / input_batch / buffers

#### 4.2.1 概念说明

这是本讲最核心的部分。v2 把状态拆成三个 Ascend 子类：

- **`AscendRequestState`**（`states.py`）：继承上游 `RequestState`，按请求维度记录状态。
- **`AscendInputBatch`**（`input_batch.py`）：一个 `@dataclass`，描述「这一次前向」的批次数据。
- **`AscendInputBuffers`**（`input_batch.py`）：预分配的、可复用的 GPU/CPU 张量缓冲区，`AscendInputBatch` 是它的一个「视图」。

这三个类都围绕**同一个核心矛盾**展开：**上游 v2 已经废弃了 CPU 端的 `seq_lens_cpu`，因为大多数 GPU 注意力后端不需要它；但 Ascend 的注意力后端仍然必须用 `seq_lens_cpu`**。所以 vllm-ascend 在三个层面把 `seq_lens_cpu`「补回来」。

> 这是理解整个 v2 Ascend 定制的钥匙：**主线 = 为 NPU 注意力后端重建 `seq_lens_cpu` 数据通路**。

#### 4.2.2 核心流程

`seq_lens_cpu` 的数据流有一条「主线」和一条「优化旁路」。主线针对投机解码（MTP）场景，旁路由 #13382 引入，针对**非投机解码**场景跳过昂贵的 D2H 拷贝与同步：

```text
上一轮前向结束（postprocess_sampled）
  ├─ 若 self.speculator is not None（MTP/投机解码激活）：
  │     └─ _copy_num_computed_tokens_to_cpu()
  │           └─ 用独立 NPU stream 把 num_computed_tokens 从 GPU 异步拷回 CPU
  │                 （投机解码会 reject token，被 reject 后「真正算到第几个 token」
  │                  只有 GPU 端才准确，必须走 D2H 才能拿到）
  └─ 否则（普通 decode，无投机）：
        └─ 跳过 D2H 拷贝（num_computed_tokens 每步确定性 += num_scheduled_tokens，
            上游父类 GPUModelRunner 的 update_requests 已经维护好了 CPU 端快照）

本轮前向开始（_update_seq_lens_cpu）
  ├─ MTP：num_computed_tokens_event.synchronize() 等 D2H 完成
  │        → num_computed_tokens_cpu[i] = num_computed_tokens_cpu[i]（读 D2H 缓冲）
  └─ 非 MTP：num_computed_tokens_cpu[i] = num_computed_tokens_np[i]（零拷贝读父类快照）
        └─ seq_lens_cpu[i] = num_computed_tokens_cpu[i] + num_scheduled_tokens[req_id]
              （= 当前请求的真实序列长度，CPU 端）
        └─ 注意力后端通过 seq_lens_np 读到它
```

两条路径的取舍逻辑很关键：**只有投机解码才需要 D2H**，因为 rejection sampling 会把部分草稿 token 拒绝掉，使「真正算到第几个 token」偏离 `+= num_scheduled_tokens` 的简单累加，而修正后的值只在 GPU 端可得。普通 decode 没有拒绝，`num_computed_tokens` 完全确定，直接读父类在 CPU 侧维护的 `num_computed_tokens_np` 即可，省掉一次 `synchronize()` 带来的 NPU 空泡。

#### 4.2.3 源码精读

**（a）AscendRequestState**。[states.py:L24-L53](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/v2/states.py#L24-L53) 在父类基础上**新增** `num_computed_tokens_cpu`：

```python
class AscendRequestState(RequestState):
    """Request state for Ascend NPUs."""
    def __init__(self, ...):
        super().__init__(...)
        # vllm gpu_model_runner_v2 deprecate the seqs_lens_cpu attribute,
        # ... Ascend attention backend must use seqs_lens_cpu,
        # so we keep num_computed_tokens_cpu here, seq_lens_cpu need to be
        # calculated by num_computed_tokens_cpu + decode_token_per_req outside.
        self.num_computed_tokens_cpu: torch.Tensor = torch.zeros(
            self.max_num_reqs, dtype=torch.int32, device="cpu")
```

注释把设计意图讲得很清楚：上游废弃了 `seq_lens_cpu`，但 NPU 后端必须用，于是这里**保留 `num_computed_tokens_cpu`**，而 `seq_lens_cpu` 留到外部（即 buffers/runner）去算。

**（b）AscendInputBuffers**。[input_batch.py:L29-L62](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/v2/input_batch.py#L29-L62) 提供实际的 CPU 缓冲：

```python
self.seq_lens_cpu: torch.Tensor = torch.zeros(max_num_reqs, dtype=torch.int32, device="cpu")
# seq_len_np 和 seq_lens_cpu 共享同一块内存；定义 seq_lens_np 方便用 numpy 计算
self.seq_lens_np: np.ndarray = self.seq_lens_cpu.numpy()
```

注意 `seq_lens_np` 与 `seq_lens_cpu` **共享内存**（`.numpy()` 是零拷贝视图），这样既能给需要 numpy 的算子用，又能给需要 torch tensor 的算子用。同时它还把 `query_start_loc` 从 `max_num_reqs+1` 扩到 `+2`，为 FULL 图模式预留填充位（见 4.5）。

**（c）AscendInputBatch**。[input_batch.py:L66-L73](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/v2/input_batch.py#L66-L73) 在上游 dataclass 基础上加两个字段：

```python
@dataclass
class AscendInputBatch(InputBatch):
    seq_lens_np: np.ndarray                       # NPU 注意力后端必需的 CPU 端 seq_lens
    attn_state: AscendAttentionState | None = None # 用于构建 attention metadata
```

它的 `make_dummy`（[input_batch.py:L76-L112](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/v2/input_batch.py#L76-L112)）还修了一个 dummy run 时的隐患：把 `num_tokens` **均匀分配**到各请求，而不是把余数全堆在最后一个请求上——否则最后一个 dummy 请求的 seq_len 会超过 `max_model_len`，导致注意力内核越界读 block table。

**（d）拷回 CPU 的两条分支（#13382 优化点）**。先看 `postprocess_sampled`，它现在用 `self.speculator is not None` 把 D2H 拷贝包了起来，见 [model_runner.py:L473-L496](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/v2/model_runner.py#L473-L496)：

```python
def postprocess_sampled(self, idx_mapping, sampled_tokens, num_sampled,
                        num_rejected, query_start_loc):
    super().postprocess_sampled(...)            # 父类完成采样后处理
    # Skip D2H copy without MTP: num_computed_tokens_cpu is synced
    # from num_computed_tokens_np in _update_seq_lens_cpu instead.
    if self.speculator is not None:
        self._copy_num_computed_tokens_to_cpu() # 只有投机解码才做 D2H
```

`_copy_num_computed_tokens_to_cpu` 本身不变（[model_runner.py:L498-L510](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/v2/model_runner.py#L498-L510)）：用一条独立 NPU stream 异步拷回 `num_computed_tokens`，并 record 一个 event。

关键差异在 `_update_seq_lens_cpu`（[model_runner.py:L512-L535](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/v2/model_runner.py#L512-L535)），它现在按「是否有 speculator」分流取数：

```python
def _update_seq_lens_cpu(self, scheduler_output, req_ids):
    num_scheduled_tokens = scheduler_output.num_scheduled_tokens
    # MTP needs D2H copy to get reverted num_computed_tokens after rejection.
    # Without MTP, num_computed_tokens_np is already correct from update_requests.
    if self.speculator is not None:
        self.num_computed_tokens_event.synchronize()                  # 等 D2H 完成
        for req_id in ...:
            self.req_states.num_computed_tokens_cpu[i] = self.num_computed_tokens_cpu[i]   # 读 D2H 缓冲
    else:
        for req_id in ...:
            self.req_states.num_computed_tokens_cpu[i] = self.req_states.num_computed_tokens_np[i]  # 读父类 CPU 快照
    # 最后统一算 seq_lens_cpu
    self.input_buffers.seq_lens_cpu[i] = num_computed_tokens + num_scheduled_tokens[req_id]
```

`num_computed_tokens_np` 是上游 `GPUModelRunner` 在每步 `update_requests` 时基于调度器值维护的 CPU 端 numpy 快照——对普通 decode 而言它就是「确定性的累加结果」，所以直接零拷贝读取即可，完全不需要 D2H。

#### 4.2.4 代码实践

**实践目标**：理解 `seq_lens_cpu` / `seq_lens_np` / `num_computed_tokens_cpu` 三者的关系，以及 #13382 的两条取数路径。

**操作步骤**（源码阅读型）：

1. 打开 `input_batch.py`，确认 `seq_lens_np = self.seq_lens_cpu.numpy()`。
2. 打开 `states.py`，找到 `num_computed_tokens_cpu` 的定义。
3. 打开 `model_runner.py` 的 `postprocess_sampled` 与 `_update_seq_lens_cpu`，看清 MTP 与非 MTP 两条分支分别从哪里取 `num_computed_tokens`。
4. 在上游 vLLM 源码里确认 `num_computed_tokens_np` 由父类 `update_requests` 维护（本步骤需翻阅上游 `vllm/v1/worker/gpu_model_runner.py`，**待本地验证**）。

**需要观察的现象**：非投机解码时，`postprocess_sampled` 不再触发任何 D2H 拷贝，`_update_seq_lens_cpu` 也不再调用 `synchronize()`；`seq_lens_cpu` 直接由「父类 CPU 快照 `num_computed_tokens_np` + 本轮调度给的 `num_scheduled_tokens`」相加而成。

**预期结果**：你能用自己的话说出「为什么普通 decode 不需要 D2H」——因为 `num_computed_tokens` 在没有 rejection 时是确定性的，父类 CPU 快照已经够准；只有投机解码会拒绝 token、让真实进度偏离累加值，才必须 D2H。

#### 4.2.5 小练习与答案

**练习 1**：`seq_lens_np` 和 `seq_lens_cpu` 为什么要共享内存？能不能各存一份？

> **参考答案**：共享内存（`.numpy()` 零拷贝）是为了让同一份数据既能被 numpy 计算路径消费（如 `np.cumsum`、索引），又能被需要 torch tensor 的注意力后端消费，避免两份拷贝带来的不一致与额外显存/内存开销。各存一份会带来同步负担和潜在 bug。

**练习 2**：为什么 #13382 只在 `self.speculator is not None` 时才做 D2H 拷贝，而不是无脑删掉？

> **参考答案**：投机解码（MTP/eagle）的 rejection sampling 会拒绝部分草稿 token，使「真正算到第几个 token」（reverted `num_computed_tokens`）偏离 `+= num_scheduled_tokens` 的简单累加；这个修正后的值只在 GPU 端可得（父类在 `postprocess_sampled` 里用 `num_rejected` 回退），所以投机解码仍必须 D2H。普通 decode 没有拒绝，父类 CPU 快照 `num_computed_tokens_np` 已经准确，省掉 D2H 与 `synchronize()` 能消除一个 NPU 空泡。

**练习 3**：`AscendInputBatch.make_dummy` 里为什么要均匀分配 token，而不是把余数堆到最后一个请求？

> **参考答案**：dummy run 用于显存 profiling / dp 探测，若把余数堆到最后一个请求，该请求的 seq_len 可能超过 `max_model_len`，导致注意力内核按 block table 读到非法页号（越界），引发 garbage page ID 或非法内存访问。

---

### 4.3 注意力元数据构建与 AscendModelState

#### 4.3.1 概念说明

上游 v2 把「构建注意力元数据」的职责放进了 `model_states/` 子系统（`DefaultModelState.prepare_attn`）。vllm-ascend 对应的子类是 `AscendModelState`（`model_states/default.py`）。它只重写一个方法 `prepare_attn`，因为 NPU 的 `build_attn_metadata` 与上游不同——它需要把上一节准备好的 `seq_lens_np`、`attn_state` 等 Ascend 专属字段塞进公共元数据 `AscendCommonAttentionMetadata`。

此外，`attn_utils.py` 里的 `build_attn_state` 负责把「这一批 token 的形状」翻译成一个枚举状态（如 `DecodeOnly` / `ChunkedPrefill`），这是 NPU 注意力后端选择内核分支的依据。

#### 4.3.2 核心流程

```text
prepare_inputs 期间
  └─ build_attn_state(seq_lens_np, num_scheduled_tokens, ...)
        └─ 判定 AscendAttentionState（PrefillNoCache / DecodeOnly / ChunkedPrefill / SpecDecoding ...）
              存入 input_batch.attn_state
前向执行前
  └─ AscendModelState.prepare_attn(input_batch, ...)
        └─ 取出 attn_state、seq_lens_np、positions 等字段
        └─ build_attn_metadata(...) 组装 AscendCommonAttentionMetadata
        └─ 每个 attention group 的 builder.build() 生成最终 metadata
```

#### 4.3.3 源码精读

**（a）build_attn_state 的判定逻辑**。[attn_utils.py:L190-L228](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/v2/attn_utils.py#L190-L228) 用一组 `if/elif` 把批次形态映射到状态：

```python
def build_attn_state(vllm_config, seq_lens_np, num_reqs, num_scheduled_tokens, num_valid_tokens):
    if vllm_config.model_config.runner_type == "pooling":
        ...  # pooling 走 PrefillNoCache / PrefillCacheHit
    elif np.array_equal(seq_lens_np[:num_reqs], num_scheduled_tokens):
        attn_state = AscendAttentionState.PrefillNoCache      # 全新 prefill，无缓存
    elif np.all(num_scheduled_tokens == 1):
        attn_state = AscendAttentionState.DecodeOnly           # 纯 decode
        ...  # mtp 投机解码时改 SpecDecoding
    elif np.all(num_valid_tokens == 1):
        attn_state = AscendAttentionState.ChunkedPrefill       # (或 SpecDecoding)
    elif vllm_config.scheduler_config.enable_chunked_prefill:
        attn_state = AscendAttentionState.ChunkedPrefill       # splitfuse
    else:
        attn_state = AscendAttentionState.PrefillCacheHit
    return attn_state
```

判定优先级很关键：先看是不是 pooling，再用 `seq_lens_np == num_scheduled_tokens` 判断「整段都是新算的」（PrefillNoCache），再看「每请求只调度 1 个 token」（DecodeOnly）……这套顺序保证了不同批次形态被正确归类。

**（b）AscendModelState.prepare_attn**。[model_states/default.py:L32-L77](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/v2/model_states/default.py#L32-L77) 只重写 `prepare_attn`：

```python
class AscendModelState(DefaultModelState):
    def prepare_attn(self, input_batch, cudagraph_mode, block_tables, slot_mappings,
                     attn_groups, kv_cache_config, for_capture=False):
        if cudagraph_mode == CUDAGraphMode.FULL:
            num_reqs = input_batch.num_reqs_after_padding     # FULL 图用填充后尺寸
            num_tokens = input_batch.num_tokens_after_padding
        else:
            num_reqs = input_batch.num_reqs                   # eager/piecewise 用真实尺寸
            num_tokens = input_batch.num_tokens
        ...
        self.attn_metadata = build_attn_metadata(
            ..., seq_lens_np=input_batch.seq_lens_np,
            positions=input_batch.positions,
            attn_state=input_batch.attn_state,                # Ascend 专属字段
            for_cudagraph_capture=for_capture)
        return self.attn_metadata
```

注意 FULL 模式下用「填充后尺寸」——这与 4.1 里 `query_start_loc` 的 `+2` 填充、4.5 的 ACL Graph 是同一套图模式约束。

**（c）工厂函数**。[model_states/__init__.py:L26-L34](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/v2/model_states/__init__.py#L26-L34) 提供 `init_asecnd_model_state`（仓库内原文拼写为 `asecnd`），供上游按需创建 Ascend 版 ModelState（注意它是延迟 import，避免循环依赖）。

#### 4.3.4 代码实践

**实践目标**：理解「批次形态 → 注意力状态」的映射。

**操作步骤**：

1. 读 `build_attn_state` 的分支顺序。
2. 设想三种批次：
   - 3 个请求，每个调度了完整 prompt（`num_scheduled_tokens == seq_lens`）。
   - 3 个请求，每个只调度 1 个 token。
   - 3 个请求，调度了若干 token 且开启了 `enable_chunked_prefill`。

**需要观察的现象**：三种情况分别命中哪个分支。

**预期结果**：情况 1 → `PrefillNoCache`；情况 2 → `DecodeOnly`（若 mtp 投机则 `SpecDecoding`）；情况 3 → `ChunkedPrefill`。

#### 4.3.5 小练习与答案

**练习 1**：`AscendModelState.prepare_attn` 在 FULL 图模式与 eager 模式下，取的 `num_reqs/num_tokens` 有什么不同？为什么？

> **参考答案**：FULL 图模式用 `num_reqs_after_padding / num_tokens_after_padding`（填充后尺寸），因为整图捕获/回放时形状必须固定；eager/piecewise 用真实尺寸。这保证回放时 attention 内核看到的形状与捕获时一致。

**练习 2**：`build_attn_state` 为什么把「`num_scheduled_tokens == 1` 且 mtp」单独判为 `SpecDecoding` 而不是 `DecodeOnly`？

> **参考答案**：mtp（multi-token prediction）投机解码在「每请求只调度 1 个 token」时仍可能涉及额外的草稿 token 验证逻辑，需要 `SpecDecoding` 内核分支支持 seq_len=1/2 的情形（注释特别提到 PD 分离场景下 SpecDecoding 需支持 seq_len=1）。

---

### 4.4 PCP 上下文并行管理器

#### 4.4.1 概念说明

**PCP（Prefill Context Parallel，预填充上下文并行）** 是一种把长 prefill 序列切分到多个 NPU 上并行计算、从而降低单卡显存与时间开销的并行策略。上游 v2 已经提供了 `PCPManager` 框架，负责把一个全局批次按 PCP 维度切成「本地子批次」。

vllm-ascend 的 `AscendPCPManager`（`pcp_manager.py`）几乎完全复用上游切分逻辑，**只重写 `partition_batch`**：因为切分后，Ascend 专属的 `seq_lens_np` 和 `attn_state` 也得跟着刷新到「本地子批次」的视角，否则本地 NPU 的注意力后端会拿到错的序列长度。

> 与 u5-l3 讲的 DCP/MLA-CP 等「注意力层内部」的上下文并行不同，PCP 是 **model_runner 层**的批次切分；两者作用层次不同。

#### 4.4.2 核心流程

```text
prepare_inputs 末尾
  └─ maybe_partition_pcp_batch(self.pcp_manager, input_batch)
        └─ AscendPCPManager.partition_batch(input_batch)
              ├─ super().partition_batch()  ← 上游把全局 batch 切成本地 batch
              └─ 刷新 Ascend 专属字段：
                    local_seq_lens_np = num_computed_tokens_np + num_scheduled_tokens
                    local_batch.attn_state = build_attn_state(...)  # 按本地视角重判状态
              └─ 返回 local_batch（视图指向本地 buffers）
```

#### 4.4.3 源码精读

`AscendPCPManager.partition_batch` 见 [pcp_manager.py:L62-L77](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/v2/pcp_manager.py#L62-L77)：

```python
def partition_batch(self, input_batch: AscendInputBatch) -> AscendInputBatch:
    """Partition the batch and update Ascend-specific local metadata."""
    local_batch = super().partition_batch(input_batch)
    assert isinstance(local_batch, AscendInputBatch)

    local_seq_lens_np = local_batch.num_computed_tokens_np + local_batch.num_scheduled_tokens
    local_batch.seq_lens_np = local_seq_lens_np
    local_batch.attn_state = build_attn_state(
        self.vllm_config, local_seq_lens_np, local_batch.num_reqs,
        local_batch.num_scheduled_tokens,
        local_batch.num_scheduled_tokens
        - (local_batch.num_draft_tokens_per_req if ... else 0))
    return local_batch
```

构造入口 [pcp_manager.py:L80-L107](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/v2/pcp_manager.py#L80-L107) 的 `maybe_build_ascend_pcp_manager`：当 `prefill_context_parallel_size <= 1` 时返回 `None`（不启用 PCP），否则校验配置并构造管理器，绑定 PCP/DCP 的 rank 与 world size。

而 runner 在 `initialize_kv_cache` 里把上游构造的社区版 PCPManager **替换**为 Ascend 版，见 [model_runner.py:L189-L201](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/v2/model_runner.py#L189-L201)：

```python
def initialize_kv_cache(self, kv_cache_config):
    with graph_manager_wrapper(self):
        super().initialize_kv_cache(kv_cache_config)
        # GPUModelRunner 在初始化 KV cache 时构造了社区版 PCP manager，这里替换为 Ascend 子类
        self.pcp_manager = maybe_build_ascend_pcp_manager(
            self.vllm_config, self.device, self.supports_mm_inputs,
            self.req_states, self.block_tables)
```

`prepare_inputs` 末尾则真正调用切分：[model_runner.py:L466](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/v2/model_runner.py#L466)。

#### 4.4.4 代码实践

**实践目标**：理解 PCP 启用条件与「本地视角刷新」。

**操作步骤**：

1. 读 `maybe_build_ascend_pcp_manager`，找到启用门槛（`pcp_size <= 1` 返回 None）。
2. 读 `partition_batch`，确认它先调 `super().partition_batch` 再刷 `seq_lens_np` 与 `attn_state`。

**需要观察的现象**：PCP 关闭时 `self.pcp_manager is None`，`maybe_partition_pcp_batch` 直接返回原 batch；PCP 开启时返回的是「本地子批次」视图。

**预期结果**：PCP 是 opt-in 的；开启后本地 batch 的 `seq_lens_np` 反映的是「本卡负责的那段序列」，而不是全局序列。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `partition_batch` 必须重新调用 `build_attn_state`？

> **参考答案**：切分后，本地子批次的 `num_scheduled_tokens` 与 `seq_lens` 都变了，原先基于全局 batch 判定的 `attn_state`（如 DecodeOnly / ChunkedPrefill）可能不再正确，必须按本地视角重新判定，否则本地注意力后端会选错内核分支。

**练习 2**：`AscendPCPManager` 与 u5-l3 的 MLA-CP / DCP 是同一层的东西吗？

> **参考答案**：不是。PCP（`pcp_manager.py`）是 **model_runner 层**的批次切分，决定「哪些 token 归哪个 NPU 算」；而 MLA-CP / DCP 是**注意力层内部**对 Q/K/V 序列维度的切分与跨卡通信。两者可以叠加，但作用层次不同。

---

### 4.5 执行主链路与 ACL Graph 接管

#### 4.5.1 概念说明

v2 的 `execute_model` / `prepare_inputs` 等主链路方法**几乎全部复用上游**，只在必要处包一层。两个最重要的「包一层」点是：

1. **FlashComm padding**：序列并行（SP）下，FlashComm1 的 reduce-scatter 要求 token 维度能被 TP size 整除；但 FULL 图的回放形状在 `prepare_inputs` 之前就已选定，padding 来不及，所以用一个上下文管理器在「选形状」之前就把 token 数向上取整。
2. **ACL Graph 接管**：把上游的 `ModelCudaGraphManager` 替换为 Ascend 的 `ModelAclGraphManager`（NPU 版 CUDA Graph），并把 `model_runner` 自身传进去，便于回放时读取 input_buffers / attn_metadata。

此外 v2 还特化了 `profile_run`（为 MC2 预留 HCCL buffer）和 `_pad_query_start_loc_for_fia`（FULL 图下满足 TND 布局约束）。

#### 4.5.2 核心流程

```text
execute_model(scheduler_output)
  └─ with flashcomm_dispatch_wrapper(...):           # SP 下提前 pad token 数
        └─ super().execute_model(...)                # 复用上游完整主链路
  └─ 若 last_pp_rank 且 FlashComm 启用：
        └─ _all_gather_hidden_states_and_aux(...)    # 把 SP 切开的 hidden states 聚回

initialize_kv_cache(kv_cache_config)
  └─ with graph_manager_wrapper(self):               # 临时把上游图管理器工厂换成 Ascend 版
        └─ super().initialize_kv_cache(...)          # 上游构造图管理器时实际拿到 ModelAclGraphManager
        └─ 替换 pcp_manager 为 AscendPCPManager
```

#### 4.5.3 源码精读

**（a）execute_model**。[model_runner.py:L204-L245](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/v2/model_runner.py#L204-L245) 用 `flashcomm_dispatch_wrapper` 包住 `super().execute_model`，并在最后做 FlashComm 的 all-gather：

```python
def execute_model(self, scheduler_output, intermediate_tensors=None, dummy_run=False, ...):
    with flashcomm_dispatch_wrapper(self.vllm_config):
        output = super().execute_model(scheduler_output, ...)
    state = self.execute_model_state
    if (self.is_last_pp_rank and state is not None
        and _flashcomm_enabled(self.vllm_config, state.input_batch.num_tokens_after_padding)):
        ... gathered_output = _all_gather_hidden_states_and_aux(state.hidden_states, num_tokens)
        self.execute_model_state = state._replace(hidden_states=hidden_states, ...)
    return output
```

FlashComm padding wrapper 见 [model_runner.py:L70-L112](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/v2/model_runner.py#L70-L112)，核心是把 `dispatch_cg_and_sync_dp` 临时替换为「先 pad 再 dispatch」的版本（注释明确说这是 TODO，等 v2 原生支持 SP 后移除）。

**（b）图管理器接管**。[model_runner.py:L581-L606](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/v2/model_runner.py#L581-L606) 用工厂替换法把 `ModelCudaGraphManager` 换成 `ModelAclGraphManager`：

```python
@contextmanager
def graph_manager_wrapper(model_runner):
    original_graph_manager = vllm_model_runner.ModelCudaGraphManager
    def factory(vllm_config, device, cudagraph_mode, decode_query_len, lora_capture_cases=None):
        return ModelAclGraphManager(vllm_config, device, cudagraph_mode,
                                    decode_query_len, model_runner,
                                    lora_capture_cases=lora_capture_cases)
    try:
        vllm_model_runner.ModelCudaGraphManager = factory
        yield
    finally:
        vllm_model_runner.ModelCudaGraphManager = original_graph_manager
```

把 `model_runner` 传入 `ModelAclGraphManager` 的原因，README 里有直接说明（[v2/README.md:L28-L33](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/v2/README.md#L28-L33)）：因为 `update_full_graph_params` 需要 runner 的 `input_buffers` 和 `model_state.attn_metadata`。

**（c）profile_run**。[model_runner.py:L248-L263](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/v2/model_runner.py#L248-L263) 在 MoE + MC2 场景下，先做一次 `mc2_tokens_capacity` 的 dummy run 来预留 HCCL buffer，再跑标准 profile_run。

**（d）FULL 图填充**。`_pad_query_start_loc_for_fia`（[model_runner.py:L542-L578](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/v2/model_runner.py#L542-L578)）满足 TND 布局约束：`hidden_states` 第一维必须等于 `actual_seq_lengths_q` 的最后一个元素。

#### 4.5.4 代码实践

**实践目标**：理解 v2 「最小侵入 + 包一层」的工程手法。

**操作步骤**：

1. 读 `execute_model`，确认它除了 `flashcomm_dispatch_wrapper` 与末尾 all-gather 外，主体完全交给 `super().execute_model`。
2. 读 `graph_manager_wrapper`，看清「工厂替换 + finally 还原」的 try/finally 模式。

**需要观察的现象**：v2 几乎不重写主链路，而是用上下文管理器在「调用上游前后」做拦截。

**预期结果**：你能总结出 v2 的定制三件套——(1) wrapper 改运行期行为（FlashComm/图管理器）；(2) 替换数据结构（三件套）；(3) 末尾补 all-gather。这是与 v1「大段重写」最大的工程差异。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `flashcomm_dispatch_wrapper` 要在 `prepare_inputs` 之前（即 dispatch 阶段）就 pad token 数，而不是在 `prepare_inputs` 里 pad？

> **参考答案**：FULL 图模式下，图的回放形状在 dispatch 阶段（`dispatch_cg_and_sync_dp`）就已选定；等到 `prepare_inputs` 再 pad 已经来不及——回放形状与实际 token 数不匹配会导致整图回放失败。所以必须在「选形状之前」就把 token 数向上取整到 TP size 的倍数。

**练习 2**：`graph_manager_wrapper` 为什么用 try/finally 还原 `ModelCudaGraphManager`？

> **参考答案**：它是对上游模块属性的全局临时替换（monkey-patch）。若不还原，后续进程内其它代码（或异常路径）会错误地拿到 Ascend 工厂；try/finally 保证无论 `super().initialize_kv_cache` 是否抛异常，上游属性都能恢复原值，避免污染全局状态。

---

### 4.6 v2 特有的 Triton 兼容修复（num_nans / penalties）

#### 4.6.1 概念说明

上游 v2 把采样相关的若干算子（`gumbel_sample`、`apply_penalties`、`compute_token_logprobs`、`apply_min_p`、`rejection_sample`、`get_num_nans` 等）都用 Triton 实现。这些算子在 GPU 上跑得好，但在 NPU/Triton-Ascend 后端上有两类问题，vllm-ascend 必须修复，否则 v2 的采样与统计路径会直接编译失败或跑飞。#13159 一次性修了其中两处：

- **`apply_penalties` 过网格（over-grid）**：当「token 数 × 词表分块数」超过 Triton-Ascend 单次 launch 的网格规模上限时，原版 kernel 会触发过网格错误。修复办法是经典的「网格分块 + kernel 内循环」。
- **`get_num_nans` 的 libdevice 解析**：上游 `get_num_nans` 这个 Triton kernel 从默认的 CUDA 取向 libdevice 函数；在 Ascend 上这会让 Triton 去解析 CUDA libdevice 符号而非 CANN 对应物，导致 kernel 编译失败。修复办法是把 `libdevice` 重绑到 CANN 版本。

这些修复都集中在一个 worker 补丁模块 `patch/worker/patch_v2/patch_triton.py` 里——它在 v2 的 worker patch 阶段被 import，把上游模块里的算子引用**重绑**到 Ascend 版实现。这正对应 u3-l1 讲的「补丁五要素」与「import 即打补丁」。

> 这也是为什么 4.1 提到「v2 仍在开发中」：上游算子要在 NPU 上稳定跑，需要逐一打补丁，#13159 就是补丁清单上新增的两条。

#### 4.6.2 核心流程

```text
worker 子进程启动（adapt_patch → worker patch 阶段）
  └─ import patch.worker.patch_v2.patch_triton
        ├─ 把上游 sample/spec 模块里的算子引用重绑到 vllm_ascend.worker.v2.* 的 Ascend 版
        │   例如 penalties.apply_penalties = ascend_apply_penalties
        └─ metrics_logits.libdevice = triton.language.extra.cann.libdevice   # num_nans 修复
              └─ 之后 sampler / rejection_sampler 里调用 get_num_nans 时，
                  Triton 编译期会拿到 CANN libdevice 符号

v2 运行期采样
  └─ apply_penalties（Ascend 版，网格已分块）
        └─ grid = (num_tokens, min(num_vocab_blocks, 65535 // num_tokens))
              └─ 每个 program 用 tl.range 迭代覆盖所有 vocab block
```

#### 4.6.3 源码精读

**（a）apply_penalties 的过网格修复**。先看 kernel 入口的网格计算，[penalties.py:L128-L149](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/v2/sample/penalties.py#L128-L149)：

```python
def apply_penalties(logits, expanded_idx_mapping, token_ids, expanded_local_pos,
                    repetition_penalty, frequency_penalty, presence_penalty,
                    prompt_bin_mask, output_bin_counts) -> None:
    num_tokens, vocab_size = logits.shape
    BLOCK_SIZE = 4096
    num_vocab_blocks = triton.cdiv(vocab_size, BLOCK_SIZE)
    vocab_grid_size = min(num_vocab_blocks, 65535 // num_tokens)   # ← 把第二维压到上限内
    _penalties_kernel[(num_tokens, vocab_grid_size)](
        ..., NUM_VOCAB_BLOCKS=num_vocab_blocks,
        VOCAB_GRID_SIZE=vocab_grid_size, BLOCK_SIZE=BLOCK_SIZE)
```

关键就是 `vocab_grid_size = min(num_vocab_blocks, 65535 // num_tokens)`：它保证 `num_tokens × vocab_grid_size ≤ 65535`，把单次 launch 的总程序数卡在上限内。那「被压掉的那部分 vocab block 谁来算」？由 kernel 内的循环兜底，[penalties.py:L61-L66](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/v2/sample/penalties.py#L61-L66)：

```python
vocab_program_idx = tl.program_id(1)
for vocab_block_idx in tl.range(
        vocab_program_idx,
        NUM_VOCAB_BLOCKS,
        VOCAB_GRID_SIZE,          # 步长 = 实际网格第二维大小，每个 program 跨多段迭代
):
    block = vocab_block_idx * BLOCK_SIZE + tl.arange(0, BLOCK_SIZE)
    ...
```

这就是「外层网格小、内层循环大」的分块模式：第二维只 launch `vocab_grid_size` 个 program，每个 program 以 `VOCAB_GRID_SIZE` 为步长迭代，最终仍能覆盖全部 `NUM_VOCAB_BLOCKS` 个 vocab block。

> 顺带一提，这个 kernel 里还有一处 NPU 专属适配：[penalties.py:L54-L56](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/v2/sample/penalties.py#L54-L56) 注释写明「NPU doesn't support chained 'or' operations like 'A or B or C'」，于是把 `use_penalty = A or B or C` 拆成两次 `or` 赋值。这也是 v2 必须自带 penalties 实现的原因之一。

**（b）get_num_nans 的 libdevice 重绑**。修复只有一行，在补丁模块 [patch_triton.py:L37](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/patch/worker/patch_v2/patch_triton.py#L37)：

```python
metrics_logits.libdevice = triton.language.extra.cann.libdevice
```

`metrics_logits` 即上游 `vllm.v1.worker.gpu.metrics.logits`，它内部定义的 `get_num_nans` kernel 在编译期会引用模块级的 `libdevice`。上游默认这个 `libdevice` 指向 CUDA 版；这一行把它改指 `triton.language.extra.cann.libdevice`，于是 sampler / rejection_sampler 调用 `get_num_nans` 时 Triton 解析到的就是 CANN 符号，编译不再失败。

**（c）补丁登记簿**。这两条修复都按 u3-l1 的规范登记在 [patch/__init__.py:L1164-L1197](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/patch/__init__.py#L1164-L1197)（第 31 条 patch）：`apply_penalties` 的 Why 写「fails on large input shape because of the triton kernel limitation」，How 写「re-write apply_penalties kernel with minimum change to support large input shape」；`get_num_nans` 的 Why 写「Triton resolve CUDA libdevice symbols instead of the CANN equivalents, causing the kernel compilation to fail」，Future Plan 都是「等上游支持 triton 算子 dispatch 后移除」。

#### 4.6.4 代码实践

**实践目标**：理解「过网格」问题与「libdevice 重绑」修复。

**操作步骤**（源码阅读型）：

1. 打开 `worker/v2/sample/penalties.py`，对照 `apply_penalties` 的 grid 计算 `vocab_grid_size = min(num_vocab_blocks, 65535 // num_tokens)`，想清楚当 `num_tokens=8`、`vocab_size=152064`（DeepSeek 词表）、`BLOCK_SIZE=4096` 时 `num_vocab_blocks` 与 `vocab_grid_size` 各是多少。
2. 打开 `patch/worker/patch_v2/patch_triton.py`，确认第 37 行的重绑，并向上看它 import 了哪些上游模块、把哪些算子重绑到 Ascend 版。
3. 打开 `patch/__init__.py` 第 31 条，核对这两条修复的 What/Why/How/Future Plan 四要素。

**需要观察的现象**：`vocab_grid_size` 永远 ≤ `65535 // num_tokens`，因此即便词表很大、batch 很大，总 launch 程序数也不会过上限；超出部分由 kernel 内的 `tl.range` 循环消化。

**预期结果**：待本地验证——若你有 NPU + Triton-Ascend 环境，可跑 `tests/e2e/nightly/single_node/ops/singlecard_ops/triton/test_penality.py`（#13159 新增的 e2e）确认大形状下 `apply_penalties` 不再报过网格错误；无 NPU 时，重点理解「外层网格 + 内层循环」的分块原理即可。

#### 4.6.5 小练习与答案

**练习 1**：为什么不直接把 `apply_penalties` 的 grid 第二维设成 `num_vocab_blocks`，而要再做一次 `min(..., 65535 // num_tokens)`？

> **参考答案**：Triton-Ascend 后端对单次 kernel launch 的网格规模（这里体现为两个维度程序数的乘积）有约 65535 的上限。当 `num_tokens × num_vocab_blocks` 超过它时（大词表 + 较大 batch），原版会过网格报错。`min` 把第二维压到上限内，再用 kernel 内的 `tl.range` 循环补算被压掉的 vocab block，从而在「不丢覆盖」的前提下规避上限。

**练习 2**：`get_num_nans` 的修复为什么只需重绑 `libdevice`，而不用改 kernel 本体？

> **参考答案**：`get_num_nans` kernel 引用的是模块级 `libdevice` 符号（如 `libdevice.isnan` 之类）。问题不在 kernel 逻辑，而在「这个符号解析到 CUDA 还是 CANN」——上游默认指向 CUDA，Ascend 上编译就失败。只要把模块级 `libdevice` 重绑到 `triton.language.extra.cann.libdevice`，kernel 内现有的所有引用在编译期就会自动解析到 CANN 符号，无需改动 kernel 本体。

---

## 5. 综合实践

**任务**：对比 v1 与 v2 model runner 的初始化差异，列出 v2 独有的模块并说明用途；并解释为何投机解码未激活时要跳过 D2H 拷贝。

**背景**：v1 的 `NPUModelRunner.__init__`（[model_runner_v1.py:L287-L581](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/model_runner_v1.py#L287-L581)）在巨型类里手工创建大量 buffer（`query_start_loc`、`group_len`、`group_key_idx`……）、`AscendSampler`、debugger、注意力状态等；v2 的 `__init__`（[model_runner.py:L119-L187](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/v2/model_runner.py#L119-L187)）则让 `super().__init__()` 干活，再做最小替换。

**操作步骤**：

1. 分别打开两个 `__init__`，逐行列出「v1 自己创建的东西」和「v2 委托给父类 + 替换的东西」。
2. 重点对照 v2 这段（[model_runner.py:L136-L165](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/v2/model_runner.py#L136-L165)）：

   ```python
   del self.req_states      # 先删，让 GC 立即回收父类创建的版本
   del self.input_buffers
   del self.speculator
   ...
   self.req_states = AscendRequestState(...)        # 替换为 Ascend 版
   self.input_buffers = AscendInputBuffers(...)     # 替换为 Ascend 版
   ```

3. 整理一张「v2 独有 / 替换模块」对照表（参考答案见下）。
4. 结合 4.2，解释 `postprocess_sampled` 里 `if self.speculator is not None` 这道闸门为什么能省掉一次 D2H 拷贝与 `synchronize()`：普通 decode 的 `num_computed_tokens` 是确定性的（每步 `+= num_scheduled_tokens`，无 rejection），父类 `update_requests` 已维护好 CPU 端 `num_computed_tokens_np`，直接读即可；只有投机解码会因为拒绝草稿 token 而偏离累加值，才必须 D2H。

**需要观察的现象**：v2 的 `__init__` 体量远小于 v1；它先 `del` 再重建三个关键属性，本质是「复用上游构造流程 + 精准替换三件套」。非投机解码路径里看不到 `_copy_num_computed_tokens_to_cpu()` 的调用，也看不到 `num_computed_tokens_event.synchronize()`。

**预期结果（参考对照表）**：

| v2 独有 / 替换模块 | 文件 | 用途 |
| --- | --- | --- |
| `AscendRequestState` | states.py | 替换上游 RequestState，保留 `num_computed_tokens_cpu`（为 seq_lens_cpu 服务） |
| `AscendInputBuffers` | input_batch.py | 替换上游 InputBuffers，新增 `seq_lens_cpu/seq_lens_np`，`query_start_loc` 扩到 +2 |
| `AscendInputBatch` | input_batch.py | 替换上游 InputBatch，新增 `seq_lens_np` 与 `attn_state` 字段 |
| `AscendModelState` | model_states/default.py | 替换上游 DefaultModelState，重写 `prepare_attn` 组装 NPU 注意力元数据 |
| `AscendPCPManager` | pcp_manager.py | 替换上游 PCPManager，切分后刷新本地 `seq_lens_np/attn_state` |
| `ModelAclGraphManager` | aclgraph_utils.py | 替换上游 ModelCudaGraphManager，做 NPU 版 ACL Graph 捕获/回放 |
| `num_computed_tokens_*` | model_runner.py | v2 独有的「GPU→CPU 异步拷回」stream/event/cpu buffer，**仅在投机解码时启用**（#13382） |
| `patch_v2/patch_triton.py` | patch/worker/ | v2 独有，重绑上游 sample/spec Triton 算子到 Ascend 版，并修复 `get_num_nans` libdevice |
| `apply_penalties`（分块版） | sample/penalties.py | v2 独有，规避 Triton-Ascend 过网格上限（#13159） |
| `flashcomm_dispatch_wrapper` | model_runner.py | v2 独有，SP 下提前 pad token 数以兼容 FULL 图 |
| `init_speculator` + `AscendEagleSpeculator` | spec_decode/ | 替换上游 speculator，v2 把投机解码做成独立 speculator 子系统 |

> 若你手头有 NPU 环境，可进一步设 `VLLM_USE_V2_MODEL_RUNNER=1` 跑一次 `examples/offline_inference_npu.py`，观察日志里那句 `npu model runner v2 is in developing...` 是否出现，以确认确实进入了 v2 分支（**待本地验证**）。

## 6. 本讲小结

- v2 的设计哲学是**「紧跟上游 v2 + 最小 Ascend 定制」**：继承上游 `GPUModelRunner`，用三件套替换 + 上下文管理器包一层，而不是像 v1 那样大面积重写。
- 启用开关被 platform 补丁简化为「只看 `VLLM_USE_V2_MODEL_RUNNER` 环境变量」，默认关闭，worker 据此在 v1/v2 runner 间分流。
- 贯穿 v2 Ascend 定制的主线矛盾是：**上游废弃了 `seq_lens_cpu`，而 NPU 注意力后端仍需要它**——于是 `AscendRequestState`/`AscendInputBatch`/`AscendInputBuffers` 三件套联手把它「补回来」。#13382 进一步优化：非投机解码时跳过 D2H 拷贝与 `synchronize()`，直接读父类 CPU 快照 `num_computed_tokens_np`，消除一个 NPU 空泡；只有投机解码（会拒绝草稿 token）才必须走 D2H。
- `AscendModelState.prepare_attn` 接管注意力元数据构建；`build_attn_state` 把批次形态翻译成 `AscendAttentionState` 枚举，驱动 NPU 注意力内核分支选择。
- PCP 是 **model_runner 层**的批次切分（区别于注意力层内部的 CP），`AscendPCPManager` 复用上游切分逻辑，只在切分后刷新本地 `seq_lens_np/attn_state`。
- 上游 v2 的采样/统计 Triton 算子在 NPU 上需要专门修复：#13159 用「网格分块 + 内层循环」解决 `apply_penalties` 过网格，用一行 `libdevice` 重绑解决 `get_num_nans` 编译失败，二者都登记在 `patch/__init__.py` 并由 `patch_v2/patch_triton.py` 应用。
- v2 是**实验性**架构，`v2/README.md` 列出了若干「待消除的差距」（cos/sin、自定义 KV cache 分配、`torch_npu_graph_wrapper` 等），部分功能（如 `dynamic_eplb`）尚未支持。

## 7. 下一步学习建议

- **深入注意力后端**：本讲多次提到 `AscendAttentionState` 与注意力元数据，下一站建议读 u5-l1（AscendAttentionBackend 注册与元数据）和 u5-l2（MLA/SFA/DSA），把「元数据如何被内核消费」补全。
- **理解 ACL Graph**：4.5 的 `ModelAclGraphManager` 只是入口，完整捕获/回放机制在 u8-l3（ACL Graph 捕获与回放）。
- **投机解码的 v2 形态**：本讲提到 `init_speculator` / `AscendEagleSpeculator` 与「仅投机解码才走 D2H」的判定，v2 把投机解码重构为独立 speculator 子系统，详见 u10-l4，以及 `worker/v2/spec_decode/` 目录。
- **补丁规范**：4.6 的两条 Triton 修复是「补丁五要素」的鲜活案例，可回到 u3-l1 与 u3-l3 把补丁登记与两阶段应用机制再串一遍。
- **亲手对照源码**：把本讲「综合实践」的对照表填满，是检验你是否真正理解 v1↔v2 差异的最佳方式；填表时若发现某项「待确认」，回到对应源码文件核对行号，不要凭记忆。
