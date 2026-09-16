# u6-l4 测试方法论：参考实现与语义等价

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 MoonEP `tests/` 目录的分层组织：`conftest.py`（环境与清理）、`kernel_test_utils.py`（参数化与断言工具箱）、`planning_reference.py`（PyTorch 参考实现）、`test_*.py`（具体测试）。
2. 掌握 GPU 分布式内核的「分参考实现对拍」方法：用一个慢但显然正确的 PyTorch 实现作为 oracle，与 CUDA 内核逐张量比较。
3. 理解为什么去重三件套（`dup_groups`/`dup_loffs`/`dup_counts`）的输出顺序不稳定，以及为什么这类输出必须比较**集合语义**而非逐元素相等。
4. 掌握 `KernelCase` 参数化机制与 `conftest.py` 的 Buffer 自动清理 fixture，并能独立为 `tests/test_planning.py` 添加一个新的测试用例。

## 2. 前置知识

本讲不涉及新的内核机制，但默认你已理解以下内容（均在前面讲义中建立）：

- **对拍（差分测试）**：为一段难于直接推理的代码（这里是跨 rank 的 CUDA 内核）写一个「慢但显然正确」的参考实现，两者吃同一份输入，比较输出是否一致。参考实现是 oracle（预言机），测试断言的是「内核 == 参考」，而不是「内核 == 某个手工算出的值」。
- **pytest 基础**：
  - `@pytest.fixture(autouse=True)`：自动应用到当前作用域内所有测试，不需要测试函数显式声明参数；
  - `yield` 型 fixture：`yield` 之前是准备、之后是清理，测试抛异常时清理代码依然执行；
  - `scope="session"`：整个测试会话只执行一次；
  - `@pytest.mark.parametrize` + `pytest.param(id=...)`：把一组用例展开成多个测试项，`id` 决定测试名。
- **分布式测试的死锁陷阱**：测试跑在 torchrun 启动的多个进程里，每个进程只看到一个 rank 的局部视图。如果 rank 0 的 `assert` 直接失败退出，而 rank 1 还在 `dist.all_gather` 里等它，整个测试组会挂起到超时。因此分布式断言必须「先汇集全体结果、再全员一起判」。
- **规划器与去重结构**（u3 系列、u4-l3）：`MoonEPCommPlan` 的字段语义、去重三件套由 dispatch 的 builder warps 用 per-warp `atomicAdd` 物化、负数 dst 编码 `-raw_dst - 1`。
- **torchrun 启动方式**（u1-l2）：测试必须由 `torchrun --nproc_per_node=N -m pytest ...` 启动，`conftest.py` 靠 `RANK` 环境变量识别。

一个贯穿本讲的关键区分：

| 比较方式 | 含义 | 适用输出 |
|---|---|---|
| 逐元素相等（`torch.equal`） | 形状、dtype、每个位置的字节都相同 | 顺序确定的输出：`dst`、`cu_seqlens`、`experts_to_copy` 等 |
| 集合语义相等 | 解析成无序集合（如映射）后相同 | 顺序不稳定的输出：`dup_groups`、`dup_loffs` |

## 3. 本讲源码地图

| 文件 | 职责 |
|---|---|
| [tests/conftest.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/conftest.py) | 两个 fixture：`dist_env`（会话级进程组/绑卡）与 `cleanup_moonep_buffers`（每个测试后自动销毁 Buffer） |
| [tests/kernel_test_utils.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/kernel_test_utils.py) | 测试基础设施核心：`KernelCase` 参数化、Buffer 生命周期注册表、路由生成封装、全 rank 断言工具箱、去重语义比较、规划不变量检查 |
| [tests/planning_reference.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/planning_reference.py) | 规划内核的 PyTorch 参考实现 `launch_planning_torch_reference`，用纯 Python 循环 + torch 张量运算复现整个规划算法 |
| [tests/test_planning.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_planning.py) | 规划测试主文件：18 个 `KernelCase` + 「对拍 + 不变量」双轨断言 + 用例覆盖元测试 |
| [tests/generate_topk_routing.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/generate_topk_routing.py) | topk 路由生成器，基准与测试共用，`bias_ratio` 控制偏置程度 |
| [moonep/planning.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py) | 被测对象的生产实现入口：`allocate_planning_outputs` 与 `launch_planning` |

## 4. 核心概念与源码讲解

本讲的三个最小模块是：PyTorch 参考实现、KernelCase 框架、规划测试。为叙述清晰，拆成四个小节：4.1 讲 KernelCase 框架与自动清理，4.2 讲参考实现，4.3 讲断言工具箱（含语义等价），4.4 讲规划测试如何把三者组装成「对拍 + 不变量」双轨。

### 4.1 KernelCase 框架：参数化用例与 Buffer 生命周期

#### 4.1.1 概念说明

分布式内核测试有三个纯工程问题要解决：

1. **一个场景有太多自由度**。S、K、专家数、H、SM 数、B、token_padding、路由模式、偏置程度、随机种子、world size 上下界——把它们散落在各个测试函数里会不可维护。MoonEP 的做法是把「一个测试场景」打包成一个不可变的值对象 `KernelCase`。
2. **Buffer 持有进程级资源**。每个 `Buffer` 底层是 VMM 物理显存、NVSwitch 组播对象、CUDA IPC fd（u2-l2/u2-l4），泄漏的 Buffer 会让同进程后续测试的分配失败。测试框架必须保证「无论测试怎么死，Buffer 都被销毁」。
3. **world size 敏感**。同一个用例在 2 卡可跑、1 卡无意义（没有远程专家）。跳过条件应该声明在用例上（`min_R`/`max_R`），而不是写在测试体里。

#### 4.1.2 核心流程

一个参数化测试的完整生命周期：

```text
torchrun 启动 N 进程
  └─ pytest 收集：PLANNING_CASES 列表 --case_params()--> N 个 pytest.param(id=用例名)
  └─ 每个测试项：
       1. dist_env fixture（session 级，首个测试触发）：
          检查 RANK 环境变量 -> init_process_group(nccl) -> 按 LOCAL_RANK 绑卡
       2. cleanup_moonep_buffers fixture（autouse，每个测试）：
          yield（测试体执行）-> destroy_active_buffers()
       3. 测试体：skip_if_unsupported_world_size -> init_case(构造 Buffer 并注册)
          -> make_topk 生成路由 -> ... 被测内核 + 参考实现 + 断言 ...
       4. teardown：无论测试是否抛异常，注册表里的 Buffer 全部 destroy
```

#### 4.1.3 源码精读

`KernelCase` 是一个 `frozen` dataclass，字段就是测试场景的全部自由度：

- [tests/kernel_test_utils.py:L24-L41](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/kernel_test_utils.py#L24-L41) — 定义 `KernelCase`：`S/K/epn/H/num_sms` 为必填，`B/token_padding/routing/bias_ratio/seed/min_R/max_R` 有默认值。注意 **E 不是字段**而是方法 `E(R) = R * epn`：专家总数取决于实际 world size，同一用例在 2 卡时 E=16、8 卡时 E=64，参考实现与内核必须在相同的 E 下对拍。`frozen` 保证用例对象在测试过程中不会被误改。

- [tests/kernel_test_utils.py:L44-L45](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/kernel_test_utils.py#L44-L45) — `case_params` 把用例列表转成带 `id=case.name` 的 `pytest.param` 列表，供 `@pytest.mark.parametrize` 消费。用例名直接成为测试名后缀，例如 `test_planning_matches_reference_and_invariants[typical_bias]`。

`init_case` 是「用例 → 运行环境」的桥梁，同时把 Buffer 登记进注册表：

- [tests/kernel_test_utils.py:L48-L65](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/kernel_test_utils.py#L48-L65) — 先做 world size 检查，然后按用例参数构造 `Buffer`，取出内部上下文 `ctx`（形状、各 meta 区偏移等，见 u2-l1），并把 buffer 存入模块级全局注册表 `_ACTIVE_BUFFERS`（[tests/kernel_test_utils.py:L13](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/kernel_test_utils.py#L13)）。`ctx["_buffer"] = buffer` 让测试也能从 ctx 反查 Buffer。

- [tests/kernel_test_utils.py:L68-L72](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/kernel_test_utils.py#L68-L72) — `destroy_active_buffers` 以栈方式弹空注册表，逐个调用 `buffer.destroy()`；`if not buffer.destroyed` 与 Buffer 销毁的幂等语义（u1-l4）配合，重复销毁是安全的。

- [tests/kernel_test_utils.py:L75-L79](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/kernel_test_utils.py#L75-L79) — `skip_if_unsupported_world_size`：低于 `min_R` 或高于 `max_R` 时 `pytest.skip`。跳过是per-用例的，同一 world size 下其他用例照常运行。

路由生成封装 `make_topk` 提供六种模式：

- [tests/kernel_test_utils.py:L82-L115](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/kernel_test_utils.py#L82-L115) — `balanced`/`biased` 委托给共享生成器（见下），其余四种是**手工构造的极端路由**：`all_local`（全部路由到本 rank 专家）、`all_remote`（全部路由到下一个 rank）、`single_expert`（所有 token 只打专家 0）、`duplicate_topk`（一个 token 的 K 个条目全是同一个远程专家——去重路径的专门刺激）。[L87-L88](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/kernel_test_utils.py#L87-L88) 的检查源于 `torch.multinomial(replacement=False)` 要求 K ≤ E。[L113-L114](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/kernel_test_utils.py#L113-L114) 统一转成 int32 连续张量并用 `bincount` 得到每专家 token 直方图 `tpe`。

随机路由的种子语义（保证可复现且多 rank 一致）：

- [tests/generate_topk_routing.py:L17-L25](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/generate_topk_routing.py#L17-L25) — 两个独立 generator：`g_shared` 以 `seed` 播种（决定专家 logit 分布与轮转置换，**全 rank 相同**，保证热门专家在各 rank 对齐），`g_local` 以 `rank` 播种（决定每个 token 的独立抽样）。这正是「对拍可复现」的根基：同 seed 重跑得到比特级相同的输入。

两个 fixture 在 `conftest.py`：

- [tests/conftest.py:L16-L33](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/conftest.py#L16-L33) — `dist_env`（session 级）：[L18-L19](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/conftest.py#L18-L19) 若环境里没有 `RANK`（即不是 torchrun 启动）就整组 skip，把「忘了用 torchrun」从报错降级为醒目的跳过；随后初始化 NCCL 进程组、按 `LOCAL_RANK` 绑卡，`yield rank, world_size` 给测试用；会话结束时 barrier 后销毁进程组。

- [tests/conftest.py:L8-L13](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/conftest.py#L8-L13) — `cleanup_moonep_buffers`（`autouse=True`，函数级）：`yield` 之后调用 `destroy_active_buffers()`。因为是 yield 型 fixture，**即使测试体抛出 AssertionError，清理也一定执行**——这是 VMM/组播资源不跨测试泄漏的关键。

#### 4.1.4 代码实践

1. **实践目标**：在不接触 GPU 的前提下，观察 `KernelCase` 参数化如何展开成测试项。
2. **操作步骤**：在仓库根目录执行：

   ```bash
   python -m pytest tests/test_planning.py --collect-only -q | head -30
   ```

3. **需要观察的现象**：输出应列出 `test_planning_step1_case_coverage`（未参数化）加上 18 个形如 `test_planning_matches_reference_and_invariants[balanced_epn16]`、`...[tiny_s1_k1]`、`...[duplicate_topk]` 的测试项，方括号里正是 `KernelCase.name`。
4. **预期结果**：共 19 个测试项。收集阶段不触发 `dist_env` fixture，所以没有 torchrun/多卡也能完成。（本讲义撰写环境未执行，**待本地验证**。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `KernelCase` 把 E 设计成方法 `E(R)` 而不是字段？

**答案**：E = R × epn 取决于实际 world size。若 E 是字段，同一个用例对象在不同 torchrun 进程数下会指向不同的专家总数，参考实现与内核就无法保证在同一个问题上对拍；写成方法后 E 由运行期 R 派生，用例本身与 world size 解耦，`min_R/max_R` 只负责声明「这个用例在哪些 R 下有意义」。

**练习 2**：如果把 `cleanup_moonep_buffers` 的清理逻辑从 yield 之后挪到测试函数末尾（每个测试自己调用 `destroy_active_buffers`），会失去什么性质？

**答案**：失去「异常路径也清理」的保证。测试断言失败（抛异常）时函数末尾的清理代码不会执行，注册表里的 Buffer 连同其 VMM 物理显存、组播对象、IPC fd 一起泄漏到下一个测试，可能让同进程后续测试分配失败；而 yield 型 fixture 的 teardown 在异常时依然运行。

**练习 3**：`make_topk` 里 `duplicate_topk` 模式生成的路由有什么测试价值？

**答案**：它让每个 token 的 K 个 top-k 条目全部落在同一个远程专家（因而同一个目的 rank）上，最大化去重路径的触发频率——这正是负数 dst 编码、builder warps、epilogue 扇出、prologue 归约这条链路的专门刺激，比随机 biased 路由更能稳定覆盖去重代码。

### 4.2 PyTorch 参考实现：launch_planning_torch_reference

#### 4.2.1 概念说明

`planning_reference.py` 是规划内核的 oracle。它的设计原则与生产内核截然相反：

- **可读性优先**：大量使用纯 Python `for` 循环和 `.item()`，逐行对应 u3 系列讲过的算法描述（Phase A 平衡 → Phase B 专家分配 → Phase C 布局与 top-B → dst 计算 → 去重编码）。慢无所谓，它只跑在小用例上。
- **确定性**：平局打破规则与内核严格一致（`torch.argmax` 取首个最大值 = 内核单 warp 循环的最小下标；top-B 排序键 `(token 数, 专家号)` 降序 = 内核的平局取大专家号），因此输出可以逐元素对拍。
- **布局适配**：数学对象与内核一致，内存布局按各自需要选最优——例如参考内部用 `[E, R]` 的 `alloc`（便于按专家迁移思考），而内核 ctx 用转置的 `[R, E]`（dest rank 连续读）；`return_alloc=True` 时参考转置返回以对齐内核缓冲，见 [tests/planning_reference.py:L73-L80](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/planning_reference.py#L73-L80) 的注释与 [L129-L131](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/planning_reference.py#L129-L131)。
- **自检**：内置守恒断言，oracle 先自证再被用作 oracle。

#### 4.2.2 核心流程

参考实现的完整数据流（与 u3 各讲一一对应）：

```text
输入: ctx（形状/偏移）、本 rank 的 topk [S,K]、tpe [E]
  1. 规范化：tpe 若是一维则 all_gather 成 [R,E]；全部搬到 CPU          (u3-l2)
  2. tpe_cumsum（沿源 rank 维前缀和）→ group_tokens（按 home group 归并）
  3. balance = group_tokens − CAP；surplus/deficit 贪心循环 → z 矩阵   (Phase A)
  4. 逐 home group：最大配额 rank ↔ 最热本地专家配对 → alloc[E,R]      (Phase B)
     → 自检：每专家 token 守恒、每 rank 不超容量
  5. alloc_cumsum；逐目的 rank：
     top-B 远程专家选择 → experts_to_copy/remote_stats                (Phase C)
     padded 段布局 → cu_seqlens/expert_off/zero_fill_ranges
  6. 逐 token 条目：局部序号 + tpe_cumsum 基数 → 全局序号 g；
     searchsorted 在 alloc_cumsum[e] 上二分 → 目的 rank → dst = dest*NvS + off
  7. 去重编码：同 token 同目的 rank 的重复条目写 −raw_dst−1；
     同时按确定性顺序生成参考去重三件套                                  (u3-l5)
  8. 返回 (dst, cu_seqlens, experts_to_copy, remote_stats,
           zero_fill_ranges, ReferenceDedupPlan)
```

#### 4.2.3 源码精读

- [tests/planning_reference.py:L25-L55](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/planning_reference.py#L25-L55) — 函数签名与输入规范化：从 `ctx` 解出 rank/R/E/B/S/K/NvS/token_padding 等；[L50-L53](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/planning_reference.py#L50-L53) 当 `tokens_per_expert` 是一维（每 rank 只有自己的直方图）时用 `dist.all_gather` 汇成 `[R,E]`——**内核走对称内存远程写、参考走 NCCL 集合通信，传输通道不同但数据相同**，这正是对拍的前提。[L54-L55](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/planning_reference.py#L54-L55) 把计算整体搬到 CPU，与 GPU 内核在设备上解耦。

- [tests/planning_reference.py:L57-L71](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/planning_reference.py#L57-L71) — Phase A 前半：`tpe.cumsum(dim=0)` 得 `tpe_cumsum`（沿**源 rank** 维前缀和，之后用于把「本 rank 专家内序号」提升为全局序号）；按 `e // epn` 归并出 `group_tokens`；`balance = group_tokens − CAP`，正为过剩、负为缺口，且 `sum(balance) = 0`。

- [tests/planning_reference.py:L83-L97](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/planning_reference.py#L83-L97) — surplus/deficit 贪心循环：每轮 `argmax`（最过剩群组）配 `argmin`（缺口最大 rank），`move = −balance[u]` 一次填满接收方后置 `balance[u]=0`。这就是 u3-l2 讲过的「z 矩阵每列至多一个非零」不变式的来源。

- [tests/planning_reference.py:L99-L127](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/planning_reference.py#L99-L127) — Phase B：对每个 home group，反复取最大剩余配额的远程 rank 与剩余 token 最多的本地专家，`take = min(rem, quota)` 拆进 `alloc`。[L124-L127](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/planning_reference.py#L124-L127) 是参考实现的**自检断言**：每专家 token 守恒、每 rank 不超容量——oracle 先证明自己满足基本定律。

- [tests/planning_reference.py:L149-L166](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/planning_reference.py#L149-L166) — Phase C 之 top-B：收集目的 rank d 上 `alloc[e,d]>0` 的非本地专家，[L160](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/planning_reference.py#L160) 按 `(token 数, 专家号)` 降序排序取前 B 个写 `experts_to_copy`（空槽保持 −1），并统计 `remote_stats`（非零远程专家数 / 每专家被预取次数）。

- [tests/planning_reference.py:L172-L205](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/planning_reference.py#L172-L205) — Phase C 之 padded 段布局：接收缓冲按 E+B 个物理段布局，被预取专家的本名段置空、token 挂到预取槽段；[L188](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/planning_reference.py#L188) 把段长向上取整到 `token_padding`，段内多出的 `n_pad` 行记入 `zero_fill_ranges`（[L197-L200](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/planning_reference.py#L197-L200)），供 dispatch 零填充 warp（u4-l3）清零。[L204-L205](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/planning_reference.py#L204-L205) 断言布局不超出 NvS 容量。

- [tests/planning_reference.py:L208-L237](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/planning_reference.py#L208-L237) — dst 计算：先用计数器给每个 token 求专家内局部序号（`local_cnt`），加上 `tpe_cumsum[rank−1, e]` 基数得全局序号 `g`；[L228](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/planning_reference.py#L228) 用 `torch.searchsorted(alloc_cumsum[e], g, right=True)` 找第一个覆盖 g 的目的 rank——这正是内核里 LOG2_R 步固定二分（u3-l4）的 torch 对应物；最后 `dst = dest*NvS + base_off + seg_pos` 完成 rank-stride 编码。

- [tests/planning_reference.py:L239-L295](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/planning_reference.py#L239-L295) — Part 3 去重：[L245-L251](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/planning_reference.py#L245-L251) 的注释说清了分工——**生产规划内核只产出 canonical dst 与 src_info，去重三件套在 fresh dispatch 的 builder warps 中物化；参考实现在这里“提前”给出结构等价、顺序确定的版本**，供 dispatch 测试做语义比较。[L292-L295](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/planning_reference.py#L292-L295) 是负数编码：组内首个条目保持非负（主槽），其余写 `−dst−1`。

- [tests/planning_reference.py:L9-L22](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/planning_reference.py#L9-L22) — `ReferenceDedupPlan` 的 docstring 明确警告：生产 builder 用 per-warp atomicAdd 分配紧凑前缀，`dup_groups`/`dup_loffs` 顺序 run-to-run 不稳定，测试必须比较**组集合**而非元素顺序——这是 4.3 节的主题。

- [tests/planning_reference.py:L297-L312](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/planning_reference.py#L297-L312) — 返回六元组：本 rank 切片（`cu_seqlens[rank]`、`zero_fill_by_rank[rank]` 等）搬回 GPU，与 `MoonEPCommPlan` 的字段一一对应。

#### 4.2.4 代码实践

1. **实践目标**：不借助任何 GPU，在纸面上把参考实现的最小用例 `tiny_s1_k1`（[tests/test_planning.py:L32](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_planning.py#L32)：S=1, K=1, epn=1, token_padding=8）完整走一遍，预先推出全部输出。
2. **操作步骤**：
   - R=1 时 E=1、B 默认为 epn=1，`generate_topk_routing` 的 balanced 分支给出 `topk=[[0]]`、`tpe=[1]`；
   - NvS 按公式 \( NvS = S \cdot K + (\text{token\_padding}-1) \cdot \frac{2E}{R} = 1 + 7 \times 2 = 15 \)；
   - `group_tokens=[1]`，`balance = 1 − CAP ≤ 0`，贪心循环立即退出，z 全零，`alloc[0,0]=1`；
   - 段布局：g=0（专家 0，未被预取）cnt=1 → padded=8 → `cu_seqlens=[8, 8]`，`expert_off[0,0]=0`，`zero_fill_ranges[0]=(1, 7)`；g=1（预取槽，`experts_to_copy[0,0]=−1`）cnt=0；
   - dst：唯一 token 的全局序号 0，`searchsorted([1], 0)=0` → `dst = 0×15 + 0 = 0`。
3. **需要观察的现象**：推出的期望输出为 `dst=[0]`、`cu_seqlens=[8,8]`、`experts_to_copy=[[-1]]`、`remote_stats=[0,0]`、`zero_fill_ranges=[[(1,7),(0,0)]]`。
4. **预期结果**：上述手推值应与多卡环境实际运行 `pytest -s "tests/test_planning.py::test_planning_matches_reference_and_invariants[tiny_s1_k1]"` 时参考实现与内核的共同输出一致（本讲义撰写环境无 GPU，**待本地验证**）。

#### 4.2.5 小练习与答案

**练习 1**：参考实现为什么把 `tpe` 用 `all_gather` 汇成 `[R,E]`，而生产内核用对称内存远程写到 rank 0？

**答案**：两者需要同样的全局信息（每个源 rank 的每专家 token 数），只是传输手段不同。参考实现面向可读性，选 NCCL 集合通信这一标准途径；生产内核面向性能，让每个 rank 把本地直方图经 NVLink 对称内存直写 rank 0 的 TPE 区（u3-l2），省去集合通信的调度开销。对拍断言的正是「不同传输通道、同样数据、同样结果」。

**练习 2**：参考实现里的自检断言（守恒、容量、NvS 上界）如果全部删掉，测试的保障会降级吗？

**答案**：会。这些断言保证 oracle 自身满足基本定律，是「参考实现对」的第一道防线；若参考实现本身错了且与内核错得一样（例如复制粘贴了同一段错误逻辑），对拍发现不了，但守恒/容量这类独立于实现细节的定律检查仍有机会暴露问题。

**练习 3**：`return_alloc=True` 分支为什么要把 `alloc` 转置成 `[R,E]` 再返回？

**答案**：内核侧 ctx 的 `alloc` 缓冲按 `alloc[d*E+e]`（dest rank 优先连续）布局，使后续按目的 rank 的读取访存连续；参考内部用 `[E,R]` 只是便于按专家思考迁移。转置返回让需要 `alloc` 的调用方（如部分 dispatch 测试）可以直接与内核缓冲逐元素比较布局也对齐的版本。

### 4.3 断言工具箱与语义等价：顺序不稳定输出必须比较集合

#### 4.3.1 概念说明

本模块解决两个测试专有难题：

**难题一：分布式断言的死锁风险。** 任何 rank 上的 `assert` 失败都会让该进程退出，其余进程还阻塞在下一个集合通信里等它。解法是 `assert_all_ranks`：把本地的通过/失败编码成张量，`all_gather` 汇总后**所有 rank 一起判**——要么全过，要么全员同时抛 AssertionError，谁也不等谁。

**难题二：顺序不稳定的输出。** 去重三件套的紧凑前缀由 builder warps 用 per-warp `atomicAdd` 分配（u4-l3），组的写入顺序取决于 warp 到达顺序，**同一次运行的不同时刻、不同次运行之间都可能不同**；而参考实现天然是确定性顺序。如果用 `torch.equal` 逐元素比较，测试会**偶发失败**（flaky test）——最糟糕的测试形态：不定期报警，最终让人忽略真报警。

解法是**语义等价比较**：把 `(dup_groups, dup_loffs)` 解析成无序映射 `{主槽 loff: 已排序的重复槽 loff 元组}` 再比较。映射的键集合无序、值内排序，对任何合法的物化顺序都稳定。

一个容易混淆的对比：`dedup_plan_fields_equal`（[tests/kernel_test_utils.py:L164-L166](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/kernel_test_utils.py#L164-L166)）确实用逐位 `torch.equal` 比较 dedup 字段——但它比较的是**同一块内存与其快照**（检查「plan 复用路径没有改动 dedup 结构」），不是两个独立产生的结构，顺序自然一致。两个不同的来源才需要语义比较。

#### 4.3.2 核心流程

语义比较的解析流程：

```text
输入: 实际 plan 与参考 plan 的三件套
  1. 逐字段核对 dtype/shape（int32、[NvS,3]/[NvS]/[2]、contiguous）
  2. 核对 dup_counts[0]（组数）与 dup_counts[1]（重复槽总数）精确相等
  3. 各自解析 _dedup_group_map：
       对每个组 (primary, dup_start, dup_count)：
         展开 dup_loffs[dup_start : dup_start+dup_count]
         → mapping[primary] = tuple(sorted(重复槽 loffs))
       同时做结构自洽检查：
         primary/重复槽范围合法、无重复行、
         primary 与重复槽互斥、计数对账
  4. 比较 mapping 字典相等（与组顺序、组内顺序均无关）
  5. assert_all_ranks 全员判
```

#### 4.3.3 源码精读

- [tests/kernel_test_utils.py:L118-L130](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/kernel_test_utils.py#L118-L130) — 基础件 `gather_tensor` 与核心断言 `assert_all_ranks`：把布尔 `ok` 编码成 CUDA 上的 int32 标量，`all_gather` 后求和，不等于 R 则全员抛错。失败时若本 rank 有 detail 就带上（[L129](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/kernel_test_utils.py#L129)），否则提示「另一个 rank 失败」——所有 rank 同时失败，无人悬挂。

- [tests/kernel_test_utils.py:L133-L150](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/kernel_test_utils.py#L133-L150) — `assert_tensor_equal_all_ranks`：CPU 上 `torch.equal` 逐位比较；失败时统计差异元素数、取出前 5 个差异位置打印 `实际值 vs 期望值`，再交给 `assert_all_ranks`。**诊断信息内建在断言里**，多卡环境下省去逐 rank 手工 inspect。

- [tests/kernel_test_utils.py:L169-L236](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/kernel_test_utils.py#L169-L236) — `_dedup_group_map`：语义比较的心脏。[L172-L174](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/kernel_test_utils.py#L172-L174) 的 docstring 直接说明动机（atomicAdd 分配、顺序不稳定、只比较组集合）。函数先钳制越界的组数/槽数并记录错误，再逐组展开 `dup_loffs` 切片、做五类结构自洽检查（[L217-L230](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/kernel_test_utils.py#L217-L230)：primary 不重复出现、组不含自身 primary、重复槽无重复行、槽数与头部计数对账、primary 集合与重复槽集合互斥），最后产出 `mapping[primary] = tuple(sorted(dups))`——**sorted 消除组内顺序，dict 消除组间顺序**。

- [tests/kernel_test_utils.py:L239-L297](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/kernel_test_utils.py#L239-L297) — `dedup_plan_semantic_errors` 与其 all-ranks 封装：先查 dtype/shape（不一致直接返回），再核对两个计标量，然后双方各自解析映射（**参考映射也要过自洽检查**——oracle 同样受审），比较映射并报告缺失/多余/不匹配的键（[L270-L282](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/kernel_test_utils.py#L270-L282)）。

- 消费点一：dispatch 测试对拍独立产生的去重结构，[tests/test_dispatch.py:L179-L182](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_dispatch.py#L179-L182) 同时调用语义比较与不变量检查；消费点二：plan 复用路径的「未改动」检查用逐位快照比较（[tests/test_e2e.py:L274-L286](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_e2e.py#L274-L286)、[tests/test_dispatch.py:L388](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_dispatch.py#L388) 与 [L412](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_dispatch.py#L412)：先 `clone_dedup_plan_fields` 快照、操作后 `dedup_plan_fields_equal` 验证字节未变）。

- 浮点输出的对应工具（本讲主线是整数规划输出，此处建立全景）：[tests/kernel_test_utils.py:L300-L303](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/kernel_test_utils.py#L300-L303) `assert_close_all_ranks` 用绝对容差；[L306-L323](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/kernel_test_utils.py#L306-L323) `bf16_ulp`/`assert_ulp_all_ranks` 按 bf16 的 ULP（\(\text{ulp}(x) = 2^{\lfloor \log_2 |x| \rfloor - 7}\)，即尾符 7 位下最后一位的权重）度量误差——u4-l6 讲过 combine 的 payload 容差源于两次 bf16 舍入，权重则是逐位相等。

#### 4.3.4 代码实践

1. **实践目标**：在纯 CPU（不需要 GPU、不需要 torchrun）上验证语义比较器确实「对顺序不敏感、对内容敏感」。
2. **操作步骤**：新建 `MoonEP-tutorial/dedup_semantics_demo.py`（示例代码，不进 tests/）：

   ```python
   import sys
   from types import SimpleNamespace
   import torch
   sys.path.insert(0, ".")
   from tests.kernel_test_utils import dedup_plan_semantic_errors

   NvS = 8
   def plan(groups, loffs, counts):
       return SimpleNamespace(
           dup_groups=torch.tensor(groups, dtype=torch.int32),
           dup_loffs=torch.tensor(loffs, dtype=torch.int32),
           dup_counts=torch.tensor(counts, dtype=torch.int32),
       )

   # 同一组重复关系 (primary=3, dups=[5,6]) 的两种物化顺序
   a = plan([[3, 0, 2]], [5, 6], [1, 2])
   b = plan([[3, 0, 2]], [6, 5], [1, 2])          # 组内顺序交换
   c = plan([[3, 1, 1], [3, 0, 1]], [6, 5], [1, 2])  # 拆成两个组：内容不同

   print("a vs b:", dedup_plan_semantic_errors("t", a, b))   # 期望 []
   print("a vs c:", dedup_plan_semantic_errors("t", a, c))   # 期望报 primary 3 重复
   ```

3. **需要观察的现象**：第一行输出空列表（语义等价：交换组内重复槽顺序不影响 `{3: (5,6)}`）；第二行输出非空错误（primary 3 出现在多个组，结构自洽检查拦截）。
4. **预期结果**：证明比较器只对「主槽 → 重复槽集合」的映射敏感。注意 `dedup_plan_semantic_errors` 是纯 CPU 函数，可以脱离 GPU 直接调用（**待本地验证**，需安装 CPU 版 torch）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `assert_all_ranks` 把布尔值转成 CUDA 张量再 all_gather，而不是直接在各 rank `assert`？

**答案**：各 rank 独立 assert 时，失败的 rank 立即退出，其余 rank 可能正阻塞在（测试后续的或断言内部的）集合通信中等待它，导致整个测试组死锁到超时。先 all_gather 汇总、再全员一致地抛错，保证所有进程同时得知失败并一起退出，无人悬挂。

**练习 2**：假设给 `dedup_plan_semantic_errors` 新增一条「`dup_groups` 前缀必须按 primary 升序排列」的检查，会发生什么？

**答案**：会引入偶发失败。生产 builder 用 atomicAdd 分配组槽位，组间顺序由 warp 到达顺序决定，不保证按 primary 升序；参考实现则是确定顺序。把顺序性写成断言等于把「实现细节的偶然」错当成「契约的必然」，正确的契约只有组集合本身。

**练习 3**：`clone_dedup_plan_fields` + `dedup_plan_fields_equal` 为什么可以用逐位相等，而参考与内核之间不行？

**答案**：前者比较的是同一块 GPU 内存在两个时刻的内容（快照 vs 现在），字节不变当且仅当逐位相等，与物化顺序无关——它检验的是「没有被改写」这一事实；后者比较的是两个独立生产者各自物化的结构，合法地可能采用不同顺序，必须先解析成无序语义对象再比较。

### 4.4 规划测试：对拍与不变量双轨

#### 4.4.1 概念说明

`test_planning.py` 把前三个模块组装成两条互补的验证轨道：

- **对拍轨**：内核输出 vs 参考实现输出，逐张量 `torch.equal`。抓「与 oracle 不一致」。
- **不变量轨**：`planning_invariant_errors` 独立于任何 oracle，直接检验输出的结构性契约（dst 可解码且在界内、cu_seqlens 单调且段长整除 token_padding、experts_to_copy 取值合法、meta 布局偏移与公式一致）。抓「违反契约」。

双轨的必要性：对拍的盲区是「参考与内核一起错」（例如两边复制了同一段错误逻辑），不变量检查独立于两者，能兜住这类共同错误；不变量的盲区是「合法但不是唯一正确解」（例如两组不同的均衡方案都可能满足全部不变量），对拍能钉住唯一解。两者叠加，测试既严又稳。

此外还有一个**元测试** `test_planning_step1_case_coverage`：不用 GPU，静态推导用例集的排序几何参数，断言用例集覆盖了 step1 排序内核的所有关键分支——「测试测试的测试集」，防止参数化用例随演化丢掉某个代码路径。

#### 4.4.2 核心流程

主测试 `test_planning_matches_reference_and_invariants` 的执行序：

```text
dist_env → (rank, R)
skip_if_unsupported_world_size(case, R)
ctx = init_case(case, R)                    # 构造 Buffer + 注册
topk, tpe = make_topk(case, rank, R)        # 共享输入
plan, cu_seqlens = allocate_planning_outputs(ctx)
launch_planning(ctx, topk.reshape(-1), tpe, cu_seqlens, plan)   # 被测内核
(ref_dst, ref_cu, ref_e2c, ref_stats, ref_zfr, _ref_dedup) =    # oracle
    launch_planning_torch_reference(ctx, topk, tpe)
对拍（逐张量、全体 rank）:
    cu_seqlens / zero_fill_ranges / experts_to_copy / remote_stats / dst
不变量:
    errors = planning_invariant_errors(case, ctx, dst, cu_seqlens, experts_to_copy)
    assert_all_ranks(not errors, ...)
```

#### 4.4.3 源码精读

- [tests/test_planning.py:L1-L5](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_planning.py#L1-L5) — 模块 docstring 给出运行命令：`torchrun --nproc_per_node=8 -m pytest -s tests/test_planning.py`。

- [tests/test_planning.py:L22-L219](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_planning.py#L22-L219) — `PLANNING_CASES` 的 18 个用例本身就是一部「边界清单」：`tiny_s1_k1`（最小规模）、`no_padding`（token_padding=1）、`near_degenerate_bias`（bias_ratio=5.0 近退化路由）、`step1_*` 三个（专门压排序内核的 CTA 划分边界，`experts_gt_block_size` 还设了 `max_R=2`）、`all_local`/`all_remote`/`single_expert`/`duplicate_topk`（四种极端路由）。命名即文档。

- [tests/test_planning.py:L267-L285](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_planning.py#L267-L285) — 被测方与 oracle 的调用：`allocate_planning_outputs` 分配 plan 与 cu_seqlens（[moonep/planning.py:L1202-L1208](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L1202-L1208)），`launch_planning` 原址填充（[moonep/planning.py:L1294-L1316](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L1294-L1316)，入口先过 `_check_planning_outputs` 与 `_check_dedup_encoding_bounds` 两道前置校验，后者即 u3-l1 讲过的位编码上限检查，如 [L1275-L1284](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L1275-L1284) 的 `S*K ≤ NvS`、`R*NvS ≤ int32_max`、`R ≤ 128`）。**内核与参考吃的是同一对 `topk`/`tpe` 张量**——输入不分叉，对拍才有意义。注意 [L284](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_planning.py#L284) 参考返回的 `_ref_dedup` 被丢弃：规划内核根本不产出去重三件套（它们由 dispatch builder 物化），所以规划测试不比它，语义比较发生在 test_dispatch。

- [tests/test_planning.py:L287-L299](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_planning.py#L287-L299) — 对拍轨：五个张量全部用 `assert_tensor_equal_all_ranks` 逐位比较。`dst` 比较前 reshape 成 `[S,K]`（[L297-L299](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_planning.py#L297-L299)）纯粹是为了失败时差异位置以 (token, k) 形式打印更可读——`torch.equal` 本身与形状视图无关。

- [tests/test_planning.py:L301-L308](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_planning.py#L301-L308) — 不变量轨：`planning_invariant_errors` 返回错误列表，空列表即通过。

- [tests/kernel_test_utils.py:L326-L409](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/kernel_test_utils.py#L326-L409) — `planning_invariant_errors` 的四组检查：
  - [L341-L362](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/kernel_test_utils.py#L341-L362)：按公式**重算** meta 布局偏移（TOPK0/ORDER/ORDER0/BARRIER/SRC_INFO），与 ctx 中的实际值核对，并检查物理 chunk 装得下 src_info——把 u2-l4 的布局契约变成可执行断言；
  - [L364-L374](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/kernel_test_utils.py#L364-L374)：dst 先解码（负值还原 `-x-1`、拆 rank 与 loff）再查界——检验的是「无论规划怎么算，编码必须可解码且在界内」；
  - [L376-L398](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/kernel_test_utils.py#L376-L398)：cu_seqlens 单调不减、**非零段长整除 token_padding**、总量不超 NvS——分组 GEMM 契约（按段整读）的直接体现；
  - [L400-L407](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/kernel_test_utils.py#L400-L407)：experts_to_copy 只能取 −1（空槽）或 [0,E) 内的专家号。

- [tests/test_planning.py:L244-L257](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_planning.py#L244-L257) — 元测试 `test_planning_step1_case_coverage`：用 `_step1_params`（[L226-L241](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_planning.py#L226-L241)，纯 Python 推导每个用例在给定 R 下的 `experts_per_block`/`s1_cols`/`work_ctas` 等排序几何）断言用例集覆盖 E>2048、s1_cols 达 512 上限、存在段尾、群组跨 CTA 等分支。**不依赖 dist_env，单进程即可运行**，是用例集演化的护栏。

#### 4.4.4 代码实践

1. **实践目标**：确认元测试可以在无 GPU 环境运行，并理解它守护的对象。
2. **操作步骤**：执行 `python -m pytest tests/test_planning.py::test_planning_step1_case_coverage -v`；然后阅读 `_step1_params`，对照 u3-l4 的排序内核几何（experts_per_block 向上对齐到 32、s1_cols 上限 512）。
3. **需要观察的现象**：测试单进程通过；打印/推导 `step1_multi_chunk_full_tile`（S=2048, epn=320）在 R=4 时的 `experts_per_block`、`work_ctas`。
4. **预期结果**：通过；E=1280、num_sms=1 时 `seg_raw=1280` → `experts_per_block=1280`、`s1_cols=512`、`work_ctas=1`、`has_segment_tail=True`——这正是用例名「multi_chunk_full_tile」要覆盖的分支（**待本地验证**）。

#### 4.4.5 小练习与答案

**练习 1**：`planning_invariant_errors` 检查「非零段长整除 token_padding」。如果内核只在个别段上漏了对齐，哪类下游故障会被这个检查提前拦截？

**答案**：分组 GEMM 按 cu_seqlens 整段读取，段长不是 token_padding 的整数倍意味着段的物理边界与逻辑边界错位，轻则读到越界行得到错误结果，重则零填充 warp（按对齐后的段边界清零）与实际数据区重叠，产生难以定位的偶发脏数据。不变量在规划输出层面就拦截，比在 GEMM 输出层面追查便宜一个数量级。

**练习 2**：为什么 `test_planning_matches_reference_and_invariants` 里参考实现的 `_ref_dedup_plan` 被丢弃，而 `test_dispatch.py` 却要拿它做语义比较？

**答案**：生产规划内核只产出 canonical dst 与 src_info，去重三件套由 fresh dispatch 的 builder warps 物化（u3-l5/u4-l3）。因此规划阶段的对拍对象不含三件套，参考实现的版本被丢弃；到 dispatch 测试时，内核侧的 builder 产出与参考版本才构成「两个独立生产者」，需要 `_dedup_group_map` 的集合语义比较。

**练习 3**：双轨中，「参考与内核一起错」的例子可能是什么样的？不变量轨如何兜住？

**答案**：例如两边的 top-B 平局规则被同一次重构一起改掉（都从「平局取大专家号」变成「取小」），对拍仍然逐位相等；但若这次重构同时破坏了「experts_to_copy 只含合法专家号或 −1」或「布局不超 NvS」，不变量轨会直接报错。不变量独立于两个实现的内部逻辑，只认契约。

## 5. 综合实践

**任务**：为 `tests/test_planning.py` 增加一个新的 `KernelCase`（epn=8、token_padding=64、biased 路由），运行它，并解释参考实现与内核输出为何逐张量相等。

### 5.1 设计参数并核对合法性

在动手前，先用约束清单核对（这些约束来自 `make_topk` 与 `launch_planning` 的前置检查）：

| 约束 | 出处 | 本用例取值 | 核对 |
|---|---|---|---|
| K ≤ E = R×epn（biased 路由 multinomial 无放回） | [tests/kernel_test_utils.py:L87-L88](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/kernel_test_utils.py#L87-L88) | K=4, R≥2 时 E≥16 | ✓ |
| S·K ≤ NvS | [moonep/planning.py:L1275-L1277](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L1275-L1277) | R=8 时 NvS = 192 + 63×16 = 1200 ≥ 192 | ✓ |
| R·NvS ≤ 2³¹−1，R ≤ 128 | [moonep/planning.py:L1278-L1284](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L1278-L1284) | 8×1200 ≪ 2³¹ | ✓ |
| padding 总量不超过 \( (\text{tp}-1) \cdot 2E/R \) 的余量 | NvS 容量公式（u3-l3） | 每目的 rank 非空段 ≤ epn+B=10，padding ≤ 63×10 < 1008 | ✓ |
| min_R=2 保证存在远程专家（否则 experts_to_copy 全 −1，用例失去意义） | — | min_R=2 | ✓ |

### 5.2 操作步骤

1. 在 `PLANNING_CASES`（[tests/test_planning.py:L22-L219](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_planning.py#L22-L219)）末尾追加（示例代码，加在自己的工作副本上）：

   ```python
   KernelCase(
       "u6l4_biased_epn8_tp64",
       S=48,
       K=4,
       epn=8,
       H=64,
       num_sms=8,
       B=2,
       token_padding=64,
       routing="biased",
       bias_ratio=1.0,
       seed=60401,
       min_R=2,
   ),
   ```

2. 无 GPU 环境先做两步本地验证（均不需要 torchrun）：

   ```bash
   python -m pytest tests/test_planning.py --collect-only -q | grep u6l4   # 应出现 1 个测试项
   python -m pytest tests/test_planning.py::test_planning_step1_case_coverage -v  # 元测试应仍通过
   ```

   第二步很重要：新增用例改变了用例集，元测试正是防止你破坏覆盖面的护栏。

3. 在多卡（NVLink 互联）环境运行单个用例：

   ```bash
   torchrun --nproc_per_node=8 -m pytest -s \
     "tests/test_planning.py::test_planning_matches_reference_and_invariants[u6l4_biased_epn8_tp64]"
   ```

### 5.3 需要观察的现象与预期结果

- 8 个进程全部通过；`-s` 下无输出即通过（断言失败才会打印差异位置与 rank）。
- 故意实验 A：把 `bias_ratio` 从 1.0 提到 5.0（近退化路由）再跑——**仍然通过**，且布局更极端（更多空段、更多 padding 行）。
- 故意实验 B：删掉 `min_R=2` 后以 `--nproc_per_node=1` 运行——通过，但可在 `-s` 下加打印观察 `experts_to_copy` 全为 −1（R=1 没有远程专家可预取），理解 `min_R` 存在的意义是保持用例的**有效性**而非**正确性**。
- 本讲义撰写环境无多卡 GPU，以上运行结果均**待本地验证**。

### 5.4 解释：为什么五个张量逐张量相等

这是本实践的点题部分，综合了全讲内容：

1. **输入不分叉**：内核与参考消费的是同一对 `topk`/`tpe` 张量（[tests/test_planning.py:L267-L285](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_planning.py#L267-L285)），不存在「两边看到不同输入」的可能；输入本身的随机性被 `g_shared`（seed）/`g_local`（rank）双 generator 钉死为可复现。
2. **算法与平局规则一致**：贪心配对中 `torch.argmax` 返回首个最大值下标，与内核单 warp 循环「平局取小下标」一致（u3-l2）；top-B 排序键 `(token 数, 专家号)` 降序与内核「平局取大专家号」一致（u3-l3）。确定性算法 + 一致平局规则 ⇒ 唯一解。
3. **全整数运算**：规划输出全是 int32 索引与计数，没有浮点舍入路径，`torch.equal` 逐位比较是恰当口径（对比 combine 的 payload 需要 ULP 容差，权重才逐位相等）。
4. **集合语义的例外被正确隔离**：唯一顺序不稳定的去重三件套不在本测试的比较范围（`_ref_dedup` 被丢弃），交给 test_dispatch 用语义比较处理——「比较口径必须匹配输出的确定性等级」正是本讲的核心方法论。

## 6. 本讲小结

- MoonEP 的测试基础设施分三层：`conftest.py` 提供环境（`dist_env` 会话级进程组）与资源安全（autouse fixture 在每个测试后弹空 `_ACTIVE_BUFFERS` 注册表，异常路径也不泄漏 VMM/组播资源）；`kernel_test_utils.py` 提供 `KernelCase` 值对象参数化、六种路由生成与全 rank 断言工具箱；`planning_reference.py` 提供 PyTorch oracle。
- 对拍方法的关键前提是「同一输入、同一确定性算法、同一平局规则」：内核与参考共享 `topk`/`tpe`，`torch.argmax` 取首最大值对应内核的最小下标平局规则，全整数输出使逐位 `torch.equal` 成为恰当的比较口径。
- `assert_all_ranks` 先 all_gather 汇总再全员同判，从机制上消灭「一个 rank 断言失败、其余 rank 死等集合通信」的分布式死锁。
- 顺序不稳定输出（builder 用 atomicAdd 物化的 `dup_groups`/`dup_loffs`）必须比较**集合语义**：解析成 `{primary: sorted(dups)}` 映射再比较；而「同一内存 vs 其快照」的未改动检查才允许逐位相等。
- 规划测试是「对拍 + 不变量」双轨：对拍钉住唯一解，不变量（dst 可解码、段长整除 token_padding、meta 偏移与公式一致等）独立于两个实现兜住共同错误；元测试 `test_planning_step1_case_coverage` 再为用例集本身的分支覆盖兜底。

## 7. 下一步学习建议

- 下一讲 u6-l5（基准测试：度量与对比方法）把视角从「正确」转向「多快」：阅读 [benchmarks/bench_comm.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_comm.py)，你会发现它复用了本讲的 `generate_topk_routing`——同一套偏置路由生成器同时服务正确性测试与性能基准。
- 若想加深本讲内容，建议带着「比较口径」的视角通读 [tests/test_dispatch.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_dispatch.py) 与 [tests/test_e2e.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_e2e.py)，标出每个断言属于「逐位 / ULP 容差 / 集合语义 / 未改动快照」中的哪一类，以及为什么。
- 更进一步：把本实践新增的用例扩展成一组 `bias_ratio ∈ {0.5, 1.0, 2.0, 5.0}` 的扫描，观察不变量检查中 padding 占比随偏置升高的趋势，为 u6-l5 的 `bias_ratio` 扫描建立直觉。
