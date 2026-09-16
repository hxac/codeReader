# u1-l4 Buffer API 快速上手

## 1. 本讲目标

前几讲我们搞清楚了 MoonEP 是什么（u1-l1）、怎么构建运行（u1-l2）、代码放在哪（u1-l3）。本讲解决下一个自然的问题：**作为使用者，怎么调用它**。

学完本讲你应该能够：

1. 准确说出 `Buffer` 构造参数 `S / H / K / E / num_ep_ranks / num_sms / token_padding / B` 的含义、默认值与约束（如 `E % num_ep_ranks == 0`）。
2. 记住 `dispatch`、`prefetch_weight`、`combine`、`reduce_grad` 四个入口的输入张量形状、dtype 约定与返回值结构，并理解同步（`async_finish=False`）与异步（`async_finish=True`）两种返回值的差异。
3. 独立写出最小的 `dispatch → prefetch_weight → combine` 调用序列，知道每一步输出的形状是什么、`plan` 为什么要保存、`buffer.destroy()` 何时调用。

本讲的立场是「先会用，再深究」：所有实现细节（对称内存、规划内核、TMA 流水线）都留到后续单元，这里只把 API 当作黑盒，但黑盒的**接口契约**必须一字不差地掌握。

## 2. 前置知识

### 2.1 进程组与 torchrun（承接 u1-l2）

MoonEP 的每个 `Buffer` 都与一个 `torch.distributed` 进程组绑定。真实使用必须由 `torchrun` 拉起多个进程，每个进程绑定一张 GPU，且卡间需要 NVLink 互联。测试目录里的标准初始化写法是：

- [tests/test_e2e.py:24-28](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_e2e.py#L24-L28)：`init_process_group(backend="nccl")` 之后，用 `LOCAL_RANK` 环境变量（测试里封装为 `local_device_index()`，见 [tests/kernel_test_utils.py:16-17](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/kernel_test_utils.py#L16-L17)）调用 `torch.cuda.set_device()` 绑卡。

### 2.2 符号速查表（承接 u1-l1）

u1-l1 已介绍过符号系统，这里作为查表备用：

| 符号 | 含义 | 典型来源 |
| --- | --- | --- |
| `S` | 每个 rank 的输入 token 数 | 模型配置 |
| `K` | 每个 token 的路由 top-k | 路由器 |
| `E` | EP 组内专家总数（均分给 R 个 rank） | 模型配置 |
| `R` | EP rank 数（`num_ep_ranks`） | 部署拓扑 |
| `B` | 每 rank 权重预取槽数 | 默认 `E // R` |
| `H` / `H'` | 隐藏维 / 专家 FFN 中间维 | 模型配置 |
| `NvS` | 每 rank 的派发槽位数 = `S×K` 真实 token + 分段 padding 余量 | 由 Buffer 推导 |
| 下标 `_sh` / `_sk` / `_nvsh` / `_nvs` | 形状后缀：`[S,H]`、`[S,K]`、`[NvS,H]`、`[NvS]` | 命名约定 |

### 2.3 四个入口在训练步中的位置

MoonEP 把一个 MoE 层的通信拆成四个入口，分别对应前向/反向的两个方向（详细的全链路在 u6-l3）：

| 象限 | 入口 | 作用 |
| --- | --- | --- |
| dispatch fwd | `dispatch` + `prefetch_weight` | 把 token 派发到远端专家分组位置；预取远程专家权重 |
| combine fwd | `combine` | 把 K 份专家输出按 token 求和归并 |
| combine bwd | `dispatch(plan=...)` | 用保存的 plan 把输出梯度重新派发（跳过规划、免预取） |
| dispatch bwd | `combine` + `reduce_grad` | 把隐藏梯度归并回 token 序；把重复专家的权重梯度归约回 home rank |

这份「四象限」的权威出处就是 api.py 的模块级文档字符串：[moonep/api.py:10-47](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L10-L47)——它是官方给出的完整用法模板，本讲第 4、5 节本质上是在逐行展开这 38 行注释。

### 2.4 CUDA event 一句话

`async_finish=True` 时，MoonEP 把内核放到内部通信流（comm stream）上执行，并返回一个 `torch.cuda.Event`。调用方在读取结果张量**之前**必须执行 `event.wait(torch.cuda.current_stream())`，否则读到的是未写完的数据。细节在 u6-l1 展开，本讲只需记住这条使用纪律。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲的用法 |
| --- | --- | --- |
| [moonep/api.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py) | `Buffer` 类与四个入口的全部定义；模块头注释是官方用法模板 | 本讲主精读对象 |
| [tests/test_e2e.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_e2e.py) | 公共 API 的端到端冒烟测试，覆盖同步/异步/plan 复用/零拷贝/梯度归约 | 官方「用法范本」，综合实践的参照 |
| [README.md](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md) | 权重/梯度缓冲布局与 API 文档口径 | 契约的文字出处（B 怎么选等） |
| [moonep/\_\_init\_\_.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/__init__.py) | 公共导出只有 `Buffer` 和 `MoonEPCommPlan` 两个名字 | 确认「用户可见面」就这么大 |
| [moonep/planning.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py) | `MoonEPCommPlan` 数据类的定义 | 本讲只当**不透明句柄**，u3-l1 逐字段精读 |

注意：`MoonEPCommPlan` 虽然是公共导出，但本讲只要求掌握「它是 dispatch 返回、需要跨前向反向保存的不可变快照」这一点，内部字段（`dst`、`experts_to_copy`、去重三件套等）留给 u3。

## 4. 核心概念与源码讲解

### 4.1 Buffer 的构造参数与资源生命周期

#### 4.1.1 概念说明

`Buffer` 是 MoonEP 唯一的顶层对象，它**拥有**（own）三类资源：

1. NVLink/VMM 对称通信缓冲（`hidden_buf`、`meta_buf`）——所有 rank 互相可读写的显存；
2. 本地 scratch 张量（规划直方图、排序缓冲、自复位屏障等）；
3. 一条异步通信流（comm stream）及四个入口共享的同步原语。

设计取向是「一次构造、长期复用」：构造时按静态形状一次性分配全部缓冲，之后每个训练步只是反复在这块内存上跑内核，不再有动态分配。这正是 u1-l1 讲过的「静态形状消除宿主同步」在 API 层的体现。因此 `Buffer` 的构造参数就是**整份内存契约**，传错一个值，后面每一步的形状都会错。

#### 4.1.2 核心流程

构造与销毁的时序：

```text
Buffer(S, H, K, E, R, ...)
  ├─ 参数校验（num_sms > 0；R == 进程组 world size；E % R == 0；B > 0）
  ├─ _create_context(...)
  │    ├─ 推导 NvS = S*K + (token_padding-1)*2*E/R
  │    ├─ 分配 NVLink 对称缓冲 hidden_buf / meta_buf（并清零屏障区）
  │    └─ 分配本地 scratch（alloc/z/local_hist/grid_sync_bar/...）
  └─ 创建 comm stream（priority=-1，高于主流）

...（整个训练周期内反复 dispatch/prefetch/combine/reduce）...

buffer.destroy()
  ├─ comm stream 同步 + torch.cuda.synchronize + 组内 barrier
  ├─ 先删视图、再删 owner（释放 VMM 映射与组播句柄）
  └─ 幂等：重复调用无副作用
```

#### 4.1.3 源码精读

构造函数签名与默认值（`num_sms=None` → 32，`B=None` → `E // num_ep_ranks`，`token_padding=128`）：

```python
def __init__(
    self,
    S: int, H: int, K: int, E: int, num_ep_ranks: int,
    num_sms: int | None = None,
    token_padding: int = 128,
    B: int | None = None,
    group: "dist.ProcessGroup | None" = None,
    comm_stream_priority: int = -1,
    enable_pdl: bool = True,
    explicitly_destroy: bool = False,
):
```

[moonep/api.py:448-461](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L448-L461) 定义了这 12 个参数；每个参数的权威解释在同一段 docstring：[moonep/api.py:463-485](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L463-L485)。README 给出的典型构造是 [README.md:73-80](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L73-L80)（`Buffer(S=4096, H=7168, K=8, E=256, num_ep_ranks=8, num_sms=32, token_padding=128)`）。

关键约束的断言位置：

- [moonep/api.py:259-262](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L259-L262)：`num_ep_ranks` 必须等于进程组的 world size——MoonEP 不允许「组比进程组小」的错配。
- [moonep/api.py:269-275](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L269-L275)：`E % R == 0`（每个 rank 恰好持有 `E/R` 个 home 专家），以及 `B` 的默认值 `B = epn = E // R`。

`NvS`（dispatch 输出的第一维）由这段代码推导：

```python
NvS_capacity = S * K
token_padding_extra = (token_padding - 1) * 2 * epn   # epn = E // R
NvS = NvS_capacity + token_padding_extra
```

[moonep/api.py:277-288](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L277-L288)。写成公式：

\[ \text{NvS} = S \cdot K + (\text{token\_padding} - 1) \cdot 2 \cdot \frac{E}{R} \]

直观解释：每个目的 rank 至多接收 `E/R` 个远程专家分段加 `E/R` 个本地专家分段；每个非空分段从「至少 1 个真实 token」向上填充到 `token_padding` 的整数倍，最坏情况下每个分段多出 `token_padding - 1` 个 padding 槽，总共至多 `2·E/R` 个分段。这就是为什么 dispatch 返回的行数 `NvS` 略大于 `S·K`——多出来的部分是分段对齐填充，其内容只在 `cu_seqlens` 覆盖的段内有定义。

`B` 怎么选，README 有明确口径（[README.md:56-59](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L56-L59)）：**训练必须 `B = E/R`**（保证 group GEMM 触及的每个专家都在本地）；**推理推荐 `B = 3~4`**，溢出时直接经对称内存从 home rank 读，慢一点但不影响正确性。

销毁：[moonep/api.py:539-543](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L539-L543) 的 docstring 说清了纪律——**必须在拆除进程组之前调用**；函数体先同步两条流、做组内 barrier，再按「先视图后 owner」的顺序释放张量（[moonep/api.py:551-580](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L551-L580)），因为张量的 Python 析构器负责归还 VMM 映射和组播句柄。它是幂等的。若担心泄漏，可传 `explicitly_destroy=True`，让「忘记 destroy 就被 GC」从静默兜底变成告警（[moonep/api.py:582-597](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L582-L597)）。

#### 4.1.4 代码实践

**实践目标**：不动 GPU，把构造参数的推导规则在纯 Python 里复现一遍，做到「给我七个参数，我能报出 Buffer 内部所有关键尺寸」。

**操作步骤**（示例代码，保存为 `buffer_params.py`，普通 `python` 即可运行）：

```python
def buffer_params(S, H, K, E, R, num_sms=None, token_padding=128, B=None):
    num_sms = 32 if num_sms is None else num_sms      # api.py:253-254 的默认
    assert E % R == 0, f"E ({E}) must be divisible by R ({R})"  # api.py:270
    epn = E // R
    B = epn if B is None else B                        # api.py:273-274 的默认
    N = S * K
    NvS = N + (token_padding - 1) * 2 * epn            # api.py:287-288
    return {
        "num_sms": num_sms, "epn(E/R)": epn, "B": B,
        "N(S*K)": N, "NvS": NvS,
        "hidden_nvsh [NvS,H]": (NvS, H),
        "cu_seqlens [E+B]": (E + B,),
        "full_weight [E+B,H,H']": "(E+B, H, H')",
        "reduce_buffer [R,B,H,H']": (R, B, "H", "H'"),
    }

if __name__ == "__main__":
    for k, v in buffer_params(S=4096, H=7168, K=8, E=256, R=8).items():
        print(f"{k:26s} = {v}")
```

**需要观察的现象**：输出中 `B` 默认等于 `E/R = 32`；`NvS = 32768 + 127×64 = 40896`，比 `S×K = 32768` 大——多出的 8128 行就是分段 padding 余量。

**预期结果**：所有数值能逐一对照 [moonep/api.py:277-288](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L277-L288) 的公式；构造真实 Buffer 时，rank 0 的日志（`_log_context_buffer_size`，[moonep/api.py:105-155](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L105-L155)）会打印同样的 `NvS` / `NvS_padded` / `B` 字段，可与脚本输出互相印证（打印日志需 `logging` 级别为 INFO，且日志行为本身待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：`E=256, R=8`，不传 `B`，`B` 是多少？训练时为什么必须是这个值？

答案：`B = E/R = 32`。规划器保证每个 rank 至多从一个远程 home group 复制专家（≤ `E/R` 个），`B = E/R` 才能保证 group GEMM 按 `cu_seqlens` 触及的每个专家都在本地有权重行。

**练习 2**：`S=4096, K=8, E=256, R=8, token_padding=128`，`NvS` 是多少？如果 `token_padding=1` 呢？

答案：`NvS = 4096×8 + (128-1)×2×32 = 32768 + 8128 = 40896`；`token_padding=1` 时余量为 0，`NvS = S×K = 32768`（任何非空分段向上取整到 1 的倍数不需要填充）。

**练习 3**：为什么构造时断言 `num_ep_ranks` 必须等于进程组 world size，而不是允许只对组内一部分 rank 生效？

答案：对称内存与组播对象的创建都以「组内全体 rank 交换句柄」为前提（u2-l2/u2-l4 详述），`_create_context` 里分配 `hidden_buf/meta_buf` 时每个 rank 都要拿到其余 `R-1` 个 rank 的物理内存映射，部分参与会让「每 rank 恰好收 `S×K` 个 token」的均衡契约无从建立。

### 4.2 dispatch：一次调用完成规划与派发

#### 4.2.1 概念说明

`dispatch` 是最核心的入口：输入本 rank 的 token 与路由结果，输出「按物理 VM 组顺序排布、可直接喂给 group GEMM」的 token 矩阵，以及一份通信计划 `plan`。它有两种互斥模式：

- **fresh planning 模式**（`plan=None`）：传入 `topk_experts_sk` 和 `tokens_per_expert`，MoonEP 在线规划（u3 的主题）后派发，返回新的 `plan` 与 `cu_seqlens`。
- **plan 复用模式**（传入已保存的 `plan`）：跳过规划，忽略 `topk_experts_sk / tokens_per_expert`，按旧计划重新散射——这正是 combine 反向（把输出梯度送回 VM 组顺序）的用法。

`plan` 的生命周期契约：**dispatch 产出，保存起来，供 prefetch_weight、combine、两个反向入口复用**。

#### 4.2.2 核心流程

同步模式下的执行序列（对应 [moonep/api.py:617-661](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L617-L661) 的编排）：

```text
dispatch(hidden_sh, route_weights_sk, topk_experts_sk, tokens_per_expert)
  ├─ (可选) launch_inter_rank_sync     # 跨 rank 预同步
  ├─ (fresh 模式) launch_planning      # 在线规划 → 产出 plan 与 cu_seqlens
  ├─ launch_dispatch                    # 按行 TMA 把 token 直写远端 expert 分组位置
  ├─ launch_dispatch_epilogue           # 本地重复行展开，得到完整 [NvS,H] 布局
  └─ (zero_copy=False) 把 NVL shard 拷贝到新分配的输出张量
```

本讲不需要理解中间三个内核，只需记住：它们都编排在这一个调用里，用户拿到的最终产物是形状静态的 `[NvS, H]` 张量。

#### 4.2.3 源码精读

签名与开关：

```python
def dispatch(
    self,
    hidden_sh: torch.Tensor,
    route_weights_sk: torch.Tensor | None = None,
    topk_experts_sk: torch.Tensor | None = None,
    tokens_per_expert: torch.Tensor | None = None,
    plan: MoonEPCommPlan | None = None,
    async_finish: bool = False,
    *,
    inter_rank_sync: bool = True,
    zero_copy: bool = False,
    router_weights_zero_copy: bool = False,
):
```

[moonep/api.py:720-732](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L720-L732)。输入契约与四个返回值的解释在 docstring：[moonep/api.py:733-781](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L733-L781)。要点：

- `hidden_sh`：`[S, H]` bf16 输入 token；
- `route_weights_sk`：`[S, K]` fp32 路由权重，传 `None` 可整体跳过权重缓冲路径；
- `topk_experts_sk`：`[S, K]` **int32** 专家 id（注意不是默认的 int64）；
- `tokens_per_expert`：`[E]` int32，**只统计本 rank 自己的 S 个 token** 中每个专家的 token 数。

fresh 与复用模式的分派逻辑：

```python
if plan is None:
    assert topk_experts_sk is not None and tokens_per_expert is not None
    ...
    plan, cu_seqlens = allocate_planning_outputs(ctx)
    planning_args = (topk_flat, tokens_per_expert, cu_seqlens)
else:
    cu_seqlens = None
    planning_args = None
    assert isinstance(plan, MoonEPCommPlan)
```

[moonep/api.py:784-795](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L784-L795)——复用模式直接把 `cu_seqlens` 置为 `None`（规划被跳过，自然没有规划产物）。

输出分配：`zero_copy=False` 时 `hidden_nvsh` 是新分配张量，`zero_copy=True` 时直接返回 NVL shard 视图（[moonep/api.py:797-808](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L797-L808)）。同步路径返回 4 元组，异步路径返回 5 元组（末尾多一个 event，[moonep/api.py:810-823](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L810-L823) 与 [moonep/api.py:825-858](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L825-L858)）。README 的 dispatch fwd 示例（含注释好的形状口径）在 [README.md:83-104](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L83-L104)。

汇总成本讲的**返回值总表**：

| 入口 | `async_finish=False` | `async_finish=True` |
| --- | --- | --- |
| `dispatch` | `(hidden_nvsh, route_weights_nvs, cu_seqlens, plan)` | 同左 + `event`（5 元组） |
| `prefetch_weight` | `None` | `event` |
| `combine` | `(hidden_sh, route_weights_sk, None)` | `(hidden_sh, route_weights_sk, event)` |
| `reduce_grad` | `None` | `event` |

#### 4.2.4 代码实践

**实践目标**：对照官方测试，写出一次 fresh dispatch 调用中每个实参的形状/dtype 清单，并验证 `tokens_per_expert` 的「本地计数」语义。

**操作步骤**：阅读 [tests/test_e2e.py:31-38](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_e2e.py#L31-L38) 的 `make_inputs`，然后手绘（或写成脚本打印）下表：

| 实参 | 生成代码 | 形状 / dtype |
| --- | --- | --- |
| `hidden_sh` | `torch.randn(S, H, dtype=torch.bfloat16)` | `[S, H]` bf16 |
| `route_weights_sk` | `torch.rand(S, K, dtype=torch.float32)` | `[S, K]` fp32 |
| `topk_experts_sk` | `torch.randint(0, E, (S, K), dtype=torch.int32)` | `[S, K]` int32 |
| `tokens_per_expert` | `torch.bincount(topk.flatten(), minlength=E).to(torch.int32)` | `[E]` int32 |

注意两点：随机数生成器用 `manual_seed(seed + rank)`——每个 rank 输入不同；`bincount` 统计的是**本 rank 的** `S×K` 个路由项，不是全局的。

**需要观察的现象**：`tokens_per_expert.sum()` 恒等于 `S×K`（每个 token 恰好贡献 K 个路由项）；单个专家的计数可以极不均衡（`randint` 均匀采样时约为 `S×K/E`，真实路由会偏得多）。

**预期结果**：写出表格后，与 [tests/test_e2e.py:232-234](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_e2e.py#L232-L234) 的实际调用 `(h_sync, w_sync, cu_sync, plan_sync) = buffer.dispatch(hidden, weights, topk, tpe)` 逐位对应。运行部分待本地验证（需多卡）。

#### 4.2.5 小练习与答案

**练习 1**：`buffer.dispatch(grad_output_sh, plan=plan)` 返回的 `cu_seqlens` 是什么？为什么？

答案：`None`。传入已保存的 `plan` 走复用路径，规划整体被跳过（[moonep/api.py:792-794](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L792-L794)），`cu_seqlens` 是规划产物，自然没有；需要的分段信息都在 `plan` 内部。

**练习 2**：`route_weights_sk=None` 调用 dispatch，返回的 `route_weights_nvs` 是什么？什么场景会这么用？

答案：`None`（[moonep/api.py:801-802](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L801-L802)），权重缓冲路径被整体裁剪。典型场景是 combine 反向重派发输出梯度——梯度不需要再乘路由权重（测试里复用路径也验证了 `w_reuse is None`，[tests/test_e2e.py:281](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_e2e.py#L281)）。

**练习 3**：`zero_copy=True` 返回的 `hidden_nvsh` 和默认模式返回的有何本质区别？使用上多了什么限制？

答案：默认模式返回新分配张量（内容从 NVL shard 拷贝而来，[moonep/api.py:797-800](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L797-L800)）；`zero_copy=True` 返回的是通信缓冲本身的视图，省掉这次拷贝，但视图会被本 Buffer 的下一次 dispatch/combine 覆盖，**不能跨通信调用存活**（尤其不能被 autograd 保存），详见 docstring [moonep/api.py:753-760](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L753-L760)（u6-l2 专题）。

### 4.3 prefetch_weight 与 combine：消费 plan 的两个入口

#### 4.3.1 概念说明

拿到 `plan` 之后有两条消费路径：

- **`prefetch_weight`（权重侧，dispatch fwd 的后半）**：把规划选中的远程热门专家权重搬进本地预取槽。权重张量的约定是「一个投影一个 `[E+B, H, H']` 连续张量」：行 `[0, E)` 是源专家权重（真实集成中它们物理上就是各 home rank 的参数内存经对称内存映射过来的，见 [README.md:47-55](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L47-L55)），行 `[E, E+B)` 是预取槽，由这次调用填充。
- **`combine`（激活侧，combine fwd / dispatch bwd）**：把专家写回 `[NvS, H]` 的输出按 token 把 K 份拷贝求和，归并回 `[S, H]` 的 token-major 输出；可选地把路由权重也收集回 `[S, K]`。它同时就是 dispatch 的反向（对偶性）。

两者的耦合点在 `cu_seqlens`：group GEMM 用 `cu_seqlens[E+B]` 决定哪些专家行（含预取槽行）在本步活跃——这就是 README 说的「MoonEP 与框架的契约：一个连续对称内存权重张量 + 一个规划产出的 `cu_seqlens`」（[README.md:45](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L45)）。

#### 4.3.2 核心流程

```text
prefetch_weight(plan, full_gate_weight, full_up_weight, full_down_weight)
  └─ 对三个权重张量（及可选 scale）依次执行：
       源区 rows[0,E) --按 plan.experts_to_copy[rank]--> 预取槽 rows[E,E+B)

combine(plan, hidden_nvsh, route_weights_nvs?)
  ├─ (zero_copy=False) 先把 hidden_nvsh / route_weights_nvs 拷入 NVL shard
  ├─ launch_combine_prologue   # 同一 token 的重复行 fp32 累加回主行
  └─ launch_combine            # 按 plan.dst 把 K 份结果求和写回 [S,H]
```

`experts_to_copy` 的形状是 `[R, B]`：每个 rank 一行、每行 B 个要复制的专家 id（负数表示空槽）。本讲只需知道「prefetch 按它决定复制谁」，其生成算法在 u3-l3。

#### 4.3.3 源码精读

`prefetch_weight` 的契约断言非常清晰地写死了权重张量约定：

```python
assert all(w is not None for w in weight_prefetch_args), \
    "prefetch_weight tensors must be provided together"
for w in weight_prefetch_args:
    assert w.dtype in _ELEM_TYPES, ...
    assert w.is_contiguous()
    assert w.ndim == 3 and int(w.shape[0]) == int(ctx['E']) + int(ctx['B'])
```

[moonep/api.py:899-907](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L899-L907)：三个权重张量必须**同时提供**、连续、第一维恰为 `E+B`。支持 bf16 与量化类型（MXFP4 时 `H'` 是 `K/2` 字节语义，u5-l2 详述），可选的 scale 张量同样遵循 `[E+B, ...]` 行约定（[moonep/api.py:909-917](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L909-L917)）。它为什么与 dispatch 分开成两个入口？docstring 给出答案：plan 复用路径（combine bwd）要**跳过重复预取**（[moonep/api.py:892-896](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L892-L896)）。

`combine` 的输入检查与零拷贝别名断言：

```python
assert tuple(hidden_nvsh.shape) == (int(ctx['NvS']), int(ctx['H']))
...
if zero_copy:
    assert hidden_nvsh.data_ptr() == ctx['hidden_buf_local'].data_ptr(), (
        "combine(zero_copy=True): hidden_nvsh must alias the NVL shard "
        "view returned by dispatch(zero_copy=True)"
    )
```

[moonep/api.py:1001-1020](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L1001-L1020)：输入必须严格 `[NvS, H]` bf16 连续；`zero_copy=True` 时用 `data_ptr()` 断言输入**精确别名** dispatch 返回的 shard 视图——不是值相等，而是同一块内存。输出 `hidden_sh` 由 MoonEP 内部 `torch.empty` 分配（[moonep/api.py:1022-1036](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L1022-L1036)），这与 dispatch 返回新张量的做法对称。README 的 combine fwd 示例在 [README.md:130-140](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L130-L140)。

#### 4.3.4 代码实践

**实践目标**：理解官方测试如何验证「预取槽 == 源专家行」这一契约，并验证 combine 的路由权重收集语义。

**操作步骤**：

1. 精读 [tests/test_e2e.py:89-100](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_e2e.py#L89-L100) 的 `assert_prefetched`：对 `experts_to_copy[rank]` 的每个非负条目 `b`，断言 `full_weight[E+b]` 与 `full_weight[expert]` 逐元素相等。
2. 再看它被调用的位置（如 [tests/test_e2e.py:241-243](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_e2e.py#L241-L243)）：`prefetch_weight` 之后 `torch.cuda.synchronize()` 再检查。
3. 阅读 [tests/test_e2e.py:331-341](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_e2e.py#L331-L341)：把 dispatch 返回的 `route_weights_nvs` 原样传回 combine，断言 `gathered_weights` 与原始输入 `weights` **逐位相等**。

**需要观察的现象**：`assert_prefetched` 的循环跳过 `expert < 0` 的槽（空槽哨兵）；权重检查是 `torch.equal`（bit-exact），因为预取是纯拷贝、无算术。

**预期结果**：能口头回答「prefetch 之后如何自检」——比对 `full_weight[E+b]` 与 `full_weight[experts_to_copy[rank][b]]`；能说出 combine 的 `gathered_route_weights_sk` 应精确还原 dispatch 输入的 `[S,K]` 权重（这是零拷贝与普通模式共同满足的性质，[tests/test_e2e.py:341](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_e2e.py#L341) 与 [tests/test_e2e.py:364-365](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_e2e.py#L364-L365) 两处断言）。运行验证需多卡环境，待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：调用 `prefetch_weight` 之前，`full_gate_weight[E:]`（预取槽）里放什么值重要吗？

答案：不重要。预取槽会被内核按 `experts_to_copy` 覆写；测试里甚至故意先填哨兵值 `7.0` 再断言「没有 prefetch 时槽位不被触碰」（[tests/test_e2e.py:103-113](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_e2e.py#L103-L113) 与 [tests/test_e2e.py:290-303](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_e2e.py#L290-L303)）。真正重要的是 `full_gate_weight[:E]`（源区）必须已经是正确内容。

**练习 2**：`combine(zero_copy=True)` 但传入的是一个值完全相同的普通张量，会发生什么？

答案：直接 `AssertionError`。零拷贝断言比较的是 `data_ptr()`（内存地址）而非内容（[moonep/api.py:1009-1013](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L1009-L1013)）；测试专门用 `assert_raises_assertion("alias", ...)` 验证了这条报错路径（[tests/test_e2e.py:366-373](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_e2e.py#L366-L373)）。

**练习 3**：为什么 `prefetch_weight` 不合并进 `dispatch`，而要单独成一个入口？

答案：两个原因（docstring [moonep/api.py:892-896](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L892-L896)）：其一，plan 复用路径（combine bwd 重派发）需要**跳过**预取；其二，异步模式下两次调用共享同一通信流，事件能表达「dispatch+prefetch 都完成」的顺序语义。

### 4.4 reduce_grad：训练侧的权重梯度归约

#### 4.4.1 概念说明

前向里被复制到各 rank 预取槽的专家，反向时会在多个 rank 上各产生一份梯度。`reduce_grad` 负责把这些重复梯度归约回专家的 home rank。它操作的内存布局是权重缓冲的 fp32 镜像（[README.md:61-69](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L61-L69)）：

- `full_*_grad`：`[E+B, H, H']` fp32 梯度缓冲，行约定与权重相同；但行 `[E, E+B)`（预取槽梯度）物理上由**独立的 reduce buffer** 支撑，而非参数梯度——重复专家的梯度是临时的，必须对框架自身的梯度归约**不可见**。
- `*_reduce_buffer`：`[R, B, H, H']` fp32——把全部 R 个 rank 的 reduce buffer 映射成一个视图，每个 rank 远程读取其它 rank 槽位里属于自己专家的梯度，累加进本地行，然后只清零自己消费过的槽位。

#### 4.4.2 核心流程

```text
reduce_grad(plan, full_{gate,up,down}_grad, {gate,up,down}_reduce_buffer)
  └─ 对三个投影依次：
       ├─ 每个目的 rank：把本 rank 预取槽梯度写入自己的 reduce_buffer[rank]
       │  （框架的反向传播已完成这件事，reduce_grad 只消费）
       ├─ owner rank 远程读所有 rank 的 reduce_buffer 中自己专家的槽位，
       │  以本地梯度为种子累加 → full_grad 的本地行
       └─ 跨 rank 屏障后，各 rank 只清零自己消费过的槽位，供下个 microbatch
```

#### 4.4.3 源码精读

签名要求六个张量**同时提供**：

```python
assert all(t is not None for t in grad_reduce_args), \
    "reduce_grad tensors must be provided together"
assert isinstance(plan, MoonEPCommPlan), "Buffer.reduce_grad: plan is required"
```

[moonep/api.py:1085-1133](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L1085-L1133)。dtype/形状的严格断言在内部分发函数里：

```python
assert full_grad.dtype == torch.float32 and full_grad.is_contiguous(), \
    f"full_{name}_grad must be contiguous fp32 [E+B, H, H']"
assert full_grad.ndim == 3 and int(full_grad.shape[0]) == E + B, ...
```

[moonep/api.py:204-208](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L204-L208)——梯度必须是 **fp32**（与权重的 bf16/量化不同）。返回值：同步模式 `None`，异步模式返回 event（[moonep/api.py:1135-1141](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L1135-L1141)）。docstring 还点出它与 combine 分离的理由：共享同一通信流以串行化 Buffer 的屏障/meta 资源（[moonep/api.py:1117-1120](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L1117-L1120)）。README 的 dispatch bwd 示例（combine 梯度 + reduce_grad 成对出现）在 [README.md:106-128](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L106-L128)。

#### 4.4.4 代码实践

**实践目标**：从测试代码反推 reduce buffer 的槽位语义与「消费后清零」契约。

**操作步骤**：

1. 阅读 [tests/test_e2e.py:128-133](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_e2e.py#L128-L133) 的 `reduce_base`：用 `arange` 的线性组合为 `[R, B, H, H']` 的每个元素构造**唯一可预测**的初值（`src`、`slot`、`row`、`col` 各自不同系数），使任何错误归约都无处遁形。
2. 阅读 [tests/test_e2e.py:187-202](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_e2e.py#L187-L202)：归约后，`reduce_buffer[rank]`（自己那一行）中被消费过的槽必须全零；其它 rank 的槽必须原封不动。

**需要观察的现象**：清零检查按 `src_rank == rank` 划分——每个 rank 只清自己的槽，这正是「只本地清零而非远程清零」的带宽权衡（u5-l3 详述）。

**预期结果**：能画出 `reduce_buffer` 的四维下标含义（`[源 rank, 预取槽 b, H, H']`）并解释为什么 `full_grad[E:]` 必须由它而非参数梯度支撑。运行验证待本地验证（需多卡）。

#### 4.4.5 小练习与答案

**练习 1**：`full_gate_grad` 与 `full_gate_weight` 的 dtype 约定有何不同？

答案：权重是 bf16（或量化类型），梯度**必须** fp32 且连续（[moonep/api.py:204-206](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L204-L206)）；两者的 `[E+B, H, H']` 行约定一致。

**练习 2**：为什么预取槽的梯度要放在独立的 reduce buffer，而不直接写进参数梯度行？

答案：重复专家的梯度是临时量，写进参数梯度会污染框架自身的梯度归约（框架按参数归属做 all-reduce，会把同一份重复梯度重复计入）；独立缓冲让 MoonEP 自己完成「归约回 home rank」这件事（[README.md:68-69](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L68-L69)）。

**练习 3**：推理场景需要调用 `reduce_grad` 吗？

答案：不需要。它是纯训练侧入口（dispatch bwd 的权重侧）；推理没有反向，也不需要 `[E+B, H, H']` 的 fp32 梯度缓冲。

### 4.5 tests/test_e2e.py：官方端到端用法范本精读

#### 4.5.1 概念说明

`test_e2e.py` 是公共 API 的冒烟测试，同时也是**最权威的用法示范**：它在一个测试函数里把同步/异步、fresh/复用、零拷贝/普通、combine/reduce_grad 全部走了一遍。读通它，本讲前四个模块的契约就全部落了地。

#### 4.5.2 核心流程

`test_e2e`（[tests/test_e2e.py:216-418](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_e2e.py#L216-L418)）的六个阶段：

```text
① 配置与构造   S=256,H=1024,K=4,E=R*4,B=2,Buffer(...)          L218-222
② 同步基准     dispatch → plan.clone() → prefetch → 校验        L232-243
③ 异步对照     dispatch(async_finish=True) → prefetch(async)   L246-261
               → event.wait → 与同步结果逐位相等
④ plan 复用    dispatch(hidden, plan=snapshot) 三连：          L274-318
               复用+预取 / 复用不预取（槽位哨兵不被碰）/ 异步复用
⑤ combine      普通 combine、带权重收集的 combine、              L323-373
               zero_copy 往返、零拷贝别名断言报错路径
⑥ 梯度归约     combine(梯度) + reduce_grad 的同步与异步两轮     L376-412
收尾            buffer.destroy() → dist.destroy_process_group() L417-418
```

#### 4.5.3 源码精读

三个最值得记住的细节：

1. **配置就在函数开头**：[tests/test_e2e.py:218-222](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_e2e.py#L218-L222)——`S, H, K, E = 256, 1024, 4, R * 4`，`B = 2`（推理式小 B 也合法），`Buffer(S, H, K, E, R, B=B, num_sms=num_sms)`。
2. **plan 快照**：`plan_snapshot = plan_sync.clone()`（[tests/test_e2e.py:238-240](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_e2e.py#L238-L240)），注释写明「防止后续 dispatch 改写 plan 内部张量」——fresh dispatch 会重建 plan 里的去重结构，做对比实验前必须快照。
3. **异步读取纪律**：`prefetch_event.wait(torch.cuda.current_stream())` 之后才 `clone()` 输出（[tests/test_e2e.py:256-258](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_e2e.py#L256-L258)），这就是 2.4 节那句话的官方示范。

#### 4.5.4 代码实践

**实践目标**：把 `test_e2e` 的六阶段流程内化为一张调用链图。

**操作步骤**：

1. 通读 [tests/test_e2e.py:216-418](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_e2e.py#L216-L418)，为每个阶段记录：入口调用、关键断言、验证的契约。
2. 若有多卡 + NVLink 环境，运行 `torchrun --nproc_per_node=8 -m pytest tests/test_e2e.py`（这是 [README.md:189](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L189) 列出的标准命令之一）。

**需要观察的现象**：测试输出末尾 rank 0 打印 `[test_e2e] PASS: public API sync/async, separate prefetch, and plan reuse match.`（[tests/test_e2e.py:414-415](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_e2e.py#L414-L415)）；任何一步契约被破坏都会以带上下文的 `AssertionError` 失败。

**预期结果**：八卡全绿。无硬件环境下，完成第 1 步的流程图即为达标；运行结果待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：测试为什么要为异步变体重新生成一套输入（`make_inputs` 第二次调用），而不是复用同步阶段的张量？

答案：注释写明「fresh inputs to avoid NVL aliasing with sync run」（[tests/test_e2e.py:245-246](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_e2e.py#L245-L246)）：`zero_copy=False` 的输出是新张量，但 NVL 通信缓冲是共享的，两次 dispatch 会先后写同一块 shard；不过由于两次输入用 `seed+rank` 生成后 `assert torch.equal(hidden, hidden2)` 验证一致，两次派发结果相同，对比才公平。

**练习 2**：`make_remote_expert`（[tests/test_e2e.py:41-66](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_e2e.py#L41-L66)）为什么不直接 `torch.randn(E, H, Hp)` 生成权重，而要绕道 `create_nvl_single_owner_tensor`？

答案：它在模拟真实集成中「行 `[0,E)` 物理上是某个 home rank 的参数内存、其他 rank 经对称内存映射看到」的布局：owner rank 物理持有数据，所有 rank 都拿到映射视图；随后 `make_full_weight` 把**别的 rank**（`owner_offset=1`）的专家数据拷进本地 `[E+B]` 张量。这保证 `assert_prefetched` 校验的是真实的跨 rank 数据搬运路径。

**练习 3**：`test_e2e` 里 `buffer.destroy()` 之后为什么还要 `dist.destroy_process_group()`？顺序能反吗？

答案：destroy 负责释放 VMM/NVLink 资源，其中包含一次组内 `dist.barrier`（[moonep/api.py:554-556](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L554-L556)）——它依赖进程组仍然存活。顺序反了会让 barrier 失效甚至挂起，这就是「destroy 必须在拆除进程组之前」的具体含义。

## 5. 综合实践

**任务**：编写 `my_first_moonep.py`——你的第一个 MoonEP 程序。初始化进程组、构造 `Buffer`、完成一次完整的 `dispatch → prefetch_weight → combine`，并打印每步输出的形状。它相当于 `test_e2e` 的「最小可用子集」：不追求数值正确性检查，只追求把三个入口的输入输出亲手喂一遍。

完整脚本（**示例代码**，参照 [tests/test_e2e.py:24-38](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_e2e.py#L24-L38) 与 [tests/test_e2e.py:216-243](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_e2e.py#L216-L243) 编写）：

```python
# my_first_moonep.py —— 运行: torchrun --nproc_per_node=8 my_first_moonep.py
import os
import torch
import torch.distributed as dist
from moonep import Buffer


def main():
    dist.init_process_group(backend="nccl")
    rank, R = dist.get_rank(), dist.get_world_size()
    torch.cuda.set_device(int(os.environ.get("LOCAL_RANK", rank)))  # 绑卡

    # 配置与 test_e2e 相同量级；E 取 R 的倍数
    S, H, K, E, B, Hp = 256, 1024, 4, R * 4, 2, 128
    buffer = Buffer(S, H, K, E, R, B=B, num_sms=32)

    # 1) 构造四个 dispatch 输入（形状/dtype 对照 4.2.4 的表格）
    g = torch.Generator(device="cuda").manual_seed(rank)   # 每 rank 不同种子
    hidden = torch.randn(S, H, dtype=torch.bfloat16, device="cuda", generator=g)
    weights = torch.rand(S, K, dtype=torch.float32, device="cuda", generator=g)
    topk = torch.randint(0, E, (S, K), dtype=torch.int32, device="cuda", generator=g)
    tpe = torch.bincount(topk.flatten(), minlength=E).to(torch.int32)

    # 2) dispatch fwd：规划 + 派发，一次拿到全部前向通信产物
    hidden_nvsh, route_weights_nvs, cu_seqlens, plan = buffer.dispatch(
        hidden, weights, topk, tpe,
    )
    print(f"[rank {rank}] dispatch    -> hidden_nvsh {tuple(hidden_nvsh.shape)} "
          f"{hidden_nvsh.dtype}, route_weights_nvs {tuple(route_weights_nvs.shape)}, "
          f"cu_seqlens {tuple(cu_seqlens.shape)}, plan {type(plan).__name__}")

    # 3) prefetch_weight：三个投影的 [E+B, H, H'] 权重（此处用普通张量演示形状，
    #    真实集成中 rows [0,E) 应是经对称内存映射的参数缓冲，见 README Weight buffer）
    full_gate = torch.randn(E + B, H, Hp, dtype=torch.bfloat16, device="cuda")
    full_up = torch.randn(E + B, H, Hp, dtype=torch.bfloat16, device="cuda")
    full_down = torch.randn(E + B, H, Hp, dtype=torch.bfloat16, device="cuda")
    buffer.prefetch_weight(plan=plan, full_gate_weight=full_gate,
                           full_up_weight=full_up, full_down_weight=full_down)
    torch.cuda.synchronize()
    print(f"[rank {rank}] prefetch    -> full_*_weight {tuple(full_gate.shape)}，"
          f"rows [E,E+B) 已按 plan.experts_to_copy[{rank}] 填充")

    # 4) 模拟专家计算：恒等 FFN，直接把派发结果当作专家输出（只演示形状流转）
    output_sh, gathered_weights_sk, _ = buffer.combine(
        plan=plan,
        hidden_nvsh=hidden_nvsh,             # zero_copy=False：先拷入 NVL shard
        route_weights_nvs=route_weights_nvs, # 同上；gathered 应逐位还原 weights
    )
    torch.cuda.synchronize()
    print(f"[rank {rank}] combine     -> output_sh {tuple(output_sh.shape)} "
          f"{output_sh.dtype}, gathered_weights_sk {tuple(gathered_weights_sk.shape)}")
    assert torch.equal(gathered_weights_sk, weights), "route weights round-trip failed"

    buffer.destroy()                          # 必须先于 destroy_process_group
    dist.destroy_process_group()


if __name__ == "__main__":
    main()
```

**预期输出**（以 R=8 为例，E=32、epn=4、NvS = 1024 + 127×2×4 = 2040；不同 R 下 `E/NvS` 随之变化）：

```text
[rank 0] dispatch    -> hidden_nvsh (2040, 1024) torch.bfloat16, route_weights_nvs (2040,), cu_seqlens (34,), plan MoonEPCommPlan
[rank 0] prefetch    -> full_*_weight (34, 1024, 128)，rows [E,E+B) 已按 plan.experts_to_copy[0] 填充
[rank 0] combine     -> output_sh (256, 1024) torch.bfloat16, gathered_weights_sk (256, 4)
```

**验收标准**：

1. 三个入口无断言错误跑通，打印形状与上表一致（`cu_seqlens` 长度 = `E+B = 34`）。
2. `gathered_weights_sk` 与输入 `weights` 逐位相等（复现 [tests/test_e2e.py:341](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_e2e.py#L341) 的断言）。
3. 能回答：为什么 `hidden_nvsh` 的第一维是 2040 而不是 1024？（答案在 4.1.3 的 NvS 公式。）

**注意**：`output_sh` 的数值**没有**语义（恒等 FFN 的 K 份求和），本实践只验证形状与调用顺序；数值级验证是 `test_e2e.py` 的职责。本脚本需要多 GPU + NVLink 环境（同 [tests/conftest.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/conftest.py) 的硬件前提），当前环境无 GPU，运行结果**待本地验证**。

## 6. 本讲小结

- `Buffer` 是一次性构造、长期复用的资源 owner：构造参数 `S/H/K/E/R` 定形状，`num_sms` 默认 32、`B` 默认 `E/R`（训练必须如此）、`token_padding` 默认 128；`NvS = S·K + (token_padding−1)·2·E/R` 决定 dispatch 输出的第一维。
- `dispatch` 一 call 三件事：在线规划（fresh 模式）→ 远端散射 → 重复展开；返回 `(hidden_nvsh [NvS,H], route_weights_nvs [NvS], cu_seqlens [E+B], plan)`；`plan` 必须保存并被四个方向复用，复用模式下 `cu_seqlens` 为 `None`。
- `prefetch_weight` 消费 `plan.experts_to_copy`，把 `[E+B,H,H']` 权重张量 rows `[0,E)` 的远程专家搬进 rows `[E,E+B)`；它与 dispatch 分离是为了 plan 复用路径能跳过预取。
- `combine` 是 dispatch 的对偶：输入 `[NvS,H]` 专家输出，输出 MoonEP 分配的 `[S,H]` token-major 结果，可选收集路由权重回 `[S,K]`；`zero_copy=True` 用 `data_ptr()` 断言精确别名。
- `reduce_grad` 是训练侧入口：`[E+B,H,H']` fp32 梯度 + `[R,B,H,H']` reduce buffer，把重复专家梯度归约回 home rank 并只清零自己消费过的槽位。
- 所有入口都支持 `async_finish=True`（返回 CUDA event，读取前必须 `wait`）；`destroy()` 幂等且必须先于 `destroy_process_group()`。

## 7. 下一步学习建议

本讲结束了你对「用户可见面」的全部探索。接下来两条路：

1. **下一讲 u2-l1（符号系统与端到端数据流）**：把本讲的形状约定下钻到 `_create_context` 的每一个公式——`NvS_padded`、meta_buf 各区段偏移、int32 溢出防护，理解这些静态尺寸是怎么算出来的。
2. **u2-l2 起**进入内存基础设施：`hidden_nvsh` 为什么能被远端 rank 直写？答案在 VMM 对称内存（`csrc/nvl_shared_buffer.cuh` 与 `moonep/buffer.py`）。

推荐同步阅读：带着本讲的疑问重读 [moonep/api.py:10-47](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L10-L47) 的用法模板——此时你应该能不看注释说出每一行的形状；再浏览 [tests/test_e2e.py:216-418](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_e2e.py#L216-L418)，确认六个阶段每一处断言你都能解释。`MoonEPCommPlan` 的内部字段则留到 u3-l1 正式拆封。
