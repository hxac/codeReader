# 作为 FLA 后端：chunk_kda 的自动分发与退出口

## 1. 本讲目标

FlashKDA 的直接入口是 `flash_kda.fwd`（u1-l5），但它在真实场景中更常见的用法是**作为 flash-linear-attention（下称 FLA）的后端被自动调用**：用户代码仍然调用 FLA 的 `chunk_kda`，一行不改，底层却已经换成 FlashKDA 的 CUDA kernel。学完本讲，你应该能够：

1. 说清这条「自动分发」链路的拓扑：谁分发、按什么条件分发、分发到谁，以及 FlashKDA 仓库在本链路中的角色（**满足后端契约，而非实现分发器**）。
2. 逐项解释 `use_gate_in_kernel` / `use_qk_l2norm_in_kernel` / `use_beta_sigmoid_in_kernel` / `safe_gate` / `transpose_state_layout` 等开关如何映射到 `flash_kda.fwd` 的接口契约，并能对照 `tests/test_fwd.py` 与 `benchmarks/bench_fwd.py` 中的真实调用验证这种映射。
3. 掌握调试与退出手段：用 `logging.INFO` 观察分发命中/拒绝日志，用环境变量 `FLA_FLASH_KDA=0` 强制回退 Triton 路径。
4. 能独立完成一次「flashkda 后端 vs Triton 路径」的输出一致性与性能对比实验。

本讲有一个需要始终牢记的前提：**分发器的代码不在本仓库里**。它在上游 FLA 仓库（[fla-org/flash-linear-attention#852](https://github.com/fla-org/flash-linear-attention/pull/852)），本仓库能做的是让 `flash_kda.fwd` 满足它要求的全部接口与语义约束。因此本讲的源码精读对象是「本仓库一侧的证据」：README 的集成章节、测试与 benchmark 中对 `chunk_kda` 的真实调用方式。

## 2. 前置知识

- **上游 / 下游与后端（backend）**：FLA 是一个聚合了多种线性注意力算子的 Python 库，同一个算子（如 `chunk_kda`）往往有多个实现后端（Triton 版、CUDA 版）。「后端」指可被替换的底层实现；「上游」指调用方所在的库。FlashKDA 对 FLA 而言是一个**外部后端**：以独立 pip 包（模块名 `flash_kda`）存在，被 FLA 在运行时导入。
- **自动分发（auto-dispatch）**：调用方代码完全不变（仍然 `from fla.ops.kda import chunk_kda`），库内部根据环境变量、参数组合、硬件能力等条件，把调用路由到某个后端。这是「drop-in replacement（即插即用替换）」的实现机制。
- **激活前 logits 与激活后数值**：u1-l2 已经建立 KDA 的门控 \( g = \text{lower\_bound} \cdot \sigma(e^{A_\text{log}} (g_\text{raw} + \text{dt\_bias})) \) 与写入强度 \( \beta = \sigma(\beta_\text{logits}) \)。`flash_kda.fwd` 的约定是 **g、beta 都传激活前的 logits，激活在 kernel 内完成**。FLA 侧对应地提供了一族 `use_*_in_kernel` 开关来声明「激活（归一化）由 kernel 负责」。本讲的核心就是这组开关的语义映射。
- **`torch.inference_mode()`**：PyTorch 的推理模式，禁用 autograd 记账（版本计数、梯度图）。`flash_kda.fwd` 是纯前向 kernel：`out` 与 `final_state` 是调用方预分配、kernel 原地写入的缓冲，路径上没有任何 autograd 注册。
- **Python `logging` 与环境变量**：`logging.basicConfig(level=logging.INFO)` 把 INFO 级日志打到 stderr；`FLA_FLASH_KDA` 是进程级环境变量，需在 Python 进程启动前设置（shell 层面 `FLA_FLASH_KDA=0 python xxx.py`）。
- **误差度量**：`get_err_ratio`（相对 RMSE）与 FLA 的 `assert_close`，见 u3-l9。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
|---|---|---|
| [README.md](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/README.md) | 项目文档 | 「Using FlashKDA as an FLA backend」整节：安装要求、调用示例、退出口、日志调试 |
| [tests/test_fwd.py](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/test_fwd.py) | 正确性测试 | `run_fla_gold_reference` / `run_flash_kda_batched`：FLA 接口与 flash_kda 接口的并排样本 |
| [benchmarks/bench_fwd.py](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/benchmarks/bench_fwd.py) | 性能基准 | `run_chunk_kda` 的对照配置——它与 README 的分发命中配置一致，由此引出「基准被分发替换」的陷阱 |
| [tests/test.sh](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/test.sh) | 测试入口 | 第 3 行安装 `flash-linear-attention>=0.5.0`，即测试环境默认具备分发能力 |

## 4. 核心概念与源码讲解

本讲的三个最小模块：**4.1 fla 集成调用方式**（分发拓扑与参数映射）、**4.2 环境开关与日志调试**（命中观察与强制退出）、**4.3 Triton 回退对比**（一致性与性能实验方法）。

### 4.1 fla 集成调用方式

#### 4.1.1 概念说明

FLA 的 `chunk_kda` 是 Triton 实现的 KDA 前向（u3-l9 中的「被替换基线」）。FlashKDA 要取代它，面临一个生态问题：大量用户代码已经写着 `chunk_kda(...)`，不可能要求他们改成 `flash_kda.fwd(...)`（两个函数的签名风格完全不同：前者返回 `(out, final_state)`，后者要求预分配 `out` 并以输出参数接收 `final_state`）。自动分发解决这一问题——分发器拦截 `chunk_kda` 调用，条件满足就转投 flashkda，条件不满足就走原来的 Triton 路径。

要理解这条链路，关键是分清**两侧的职责**：

- **上游 FLA（分发器，不在本仓库）**：检查环境变量、参数组合、硬件能力；命中时导入 `flash_kda` 并按后端契约组装调用；未命中时回退 Triton。
- **本仓库（后端）**：提供满足契约的 `flash_kda.fwd`——固定的张量布局、dtype、激活语义、状态布局——并在 README 中写清「要命中分发，调用方必须如何传参」。

因此本模块的「源码精读」读的不是分发器本身，而是本仓库中**对契约的三份独立陈述**：README（面向用户的契约说明）、`benchmarks/bench_fwd.py`（性能口径下的契约样本）、`tests/test_fwd.py`（正确性口径下的契约样本与历史演进痕迹）。

#### 4.1.2 核心流程

分发链路的拓扑（依据 README 的描述整理；判定细节属上游实现，本仓库不可见）：

```text
用户代码
  │  with torch.inference_mode():
  │      out, final_state = chunk_kda(q,k,v,g,beta, ..., use_*_in_kernel=True, ...)
  ▼
fla.ops.kda.chunk_kda          ← 上游入口（Triton 版 KDA）
  │
  ├─ 分发器检查（上游实现，PR #852）：
  │    ① 环境变量 FLA_FLASH_KDA 是否为 0（退出开关）
  │    ② 参数组合是否满足后端契约（use_*_in_kernel 开关族、状态布局等）
  │    ③ 其他条件（硬件/版本，具体清单待确认，属上游）
  │
  ├─ 命中：[FLA Backend] kda.chunk_kda -> flashkda
  │        按 flash_kda.fwd 契约组装调用（预分配 out/final_state、映射参数）
  │        → K1（prepare）→ workspace → K2（recurrence）   ← u1-l4 的调用链
  │
  └─ 未命中：日志 ... rejected: <reason>
           → 原 Triton chunk_kda 路径
```

参数到契约的映射表（左列是 FLA 侧开关/参数，右列是它对 `flash_kda.fwd` 契约的含义；「契约依据」列指向本讲 4.1.3 精读的源码行）：

| FLA `chunk_kda` 侧 | 对 flashkda 后端的含义 | 契约依据 |
|---|---|---|
| `use_gate_in_kernel=True` + `A_log`/`dt_bias`/`lower_bound` | g 传**激活前 logits**，门控激活 \( e^{A_\text{log}} \)、sigmoid、lower_bound 缩放在 K1 内完成 | README 参数表（`g`: gate before activation） |
| `use_qk_l2norm_in_kernel=True` | q/k **无需预先 L2 归一化**，归一化在 K1 内完成（u2-l7 模块①） | `run_flash_kda_batched` 直接喂原始 `torch.rand` 的 q |
| `use_beta_sigmoid_in_kernel=True` | beta 传**激活前 logits**，sigmoid 在 K2 Phase 2 内完成 | README 参数表（`beta`: pre-activation; sigmoid applied internally） |
| `safe_gate=True` | 上游侧门控安全开关，语义由 FLA 定义，本仓库源码不涉及 | 仅 README 示例要求携带 |
| `transpose_state_layout=True` | FLA 的状态张量切换为 `[B,H,V,K]`（batched）/`[N,H,V,K]`（varlen），与 `flash_kda.fwd` 的状态布局对齐 | 测试中两种状态可直接相减 |
| `output_final_state=True` | 对应 `flash_kda.fwd` 的 `final_state` 输出参数（接口风格差异：返回值 vs 预分配写入） | README 示例 |
| `initial_state=h0`（dtype 任选 bf16/fp32） | 决定 flashkda 内部走 `StateFP32` 分支与否（u2-l3 的 7 分支之一） | bench 中 `h0_ck = initial_state.float()` |
| `cu_seqlens=...` | varlen 模式，B=1，状态形状 `[N,H,V,K]` | README 参数表 |
| `torch.inference_mode()` 上下文 | 使用约束：纯前向、原地写 out、无 autograd | README 要求；测试同样遵守 |
| `K = V = 128` | flashkda 的硬约束（README：Currently requires K = V = 128） | README 参数表 |

#### 4.1.3 源码精读

**① README 集成章节——契约的权威陈述。** 整节说明了「装好即用、条件命中、可退出、可调试」四件事：

[README.md:30-32](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/README.md#L30-L32) —— 声明 FlashKDA 装好后会被 FLA 的 `chunk_kda` 自动分发使用，并给出上游集成 PR 的链接（分发器实现所在地）。

[README.md:40-60](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/README.md#L40-L60) —— 完整调用样本：`torch.inference_mode()` 上下文 + 全部开关（`use_gate_in_kernel=True`、`use_qk_l2norm_in_kernel=True`、`use_beta_sigmoid_in_kernel=True`、`safe_gate=True`、`transpose_state_layout=True`）+ `A_log`/`dt_bias`/`lower_bound` + `cu_seqlens`。注意它展示的正是 varlen 模式（`cu_seqlens` 传入），且返回值是 `(out, final_state)` 二元组——与 `flash_kda.fwd` 的「预分配输出」风格形成对照。

[README.md:62-64](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/README.md#L62-L64) —— 两个运维接口：`FLA_FLASH_KDA=0` 退回 Triton；`logging.basicConfig(level=logging.INFO)` 查看命中日志 `[FLA Backend] kda.chunk_kda -> flashkda` 或未命中日志 `... rejected: <reason>`（4.2 模块的主题）。

**② 参数表——激活语义与状态布局的契约。**

[README.md:92-104](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/README.md#L92-L104) —— `flash_kda.fwd` 的参数规格：`g` 标注为 *Gate before activation*（L95），`beta` 标注为 *Beta logits (pre-activation; sigmoid applied internally)*（L96），状态形状 `[B, H, V, K]` / varlen 下 `[N, H, V, K]`（L102-104），`K = V = 128` 硬约束（L106）。这四条正是映射表中 `use_gate_in_kernel` / `use_beta_sigmoid_in_kernel` / `transpose_state_layout` 三行开关的落点：**FLA 侧开关为 True 时，用户传给 `chunk_kda` 的张量语义（原始 logits、未归一化 q/k、转置状态布局）与 `flash_kda.fwd` 的要求完全一致，分发器无需做任何数值预处理，只做布局组装**。

**③ bench 中的对照配置——性能口径下的同一样本。**

[benchmarks/bench_fwd.py:98-111](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/benchmarks/bench_fwd.py#L98-L111) —— `run_chunk_kda` 的调用参数与 README 分发示例一字不差（`use_gate_in_kernel=True`、`use_qk_l2norm_in_kernel=True`、`use_beta_sigmoid_in_kernel=True`、`A_log`/`dt_bias`/`lower_bound`、`transpose_state_layout=True`、`**extra` 携带 `cu_seqlens`）。另注意 L96：`h0_ck = initial_state.float()`——传给 `chunk_kda` 的初始状态是 **fp32**，若分发命中，后端走的正是 u2-l3 七分支中的 `state_fp32` 实例。这个「基线配置 = 分发命中配置」的事实有一个重要推论，留到 4.2.3 ③ 展开。

**④ 测试中的 FLA 调用——正确性口径 + 一段历史演进。**

[tests/test_fwd.py:71-83](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/test_fwd.py#L71-L83) —— `run_fla_gold_reference` 开头：从 `fla.ops.kda` 导入 `chunk_kda` 与 `fused_recurrent_kda`（L75），然后在 **Python 侧手动完成激活**——`g_activated_fp64 = lower_bound * sigmoid(exp(A_log) * (g + dt_bias))`（L78-80）、`beta_activated_fp64 = sigmoid(beta)`（L83）。这是理解开关族语义的最佳反衬：当激活**不在 kernel 内**做时，调用方必须自己先激活。

[tests/test_fwd.py:87-102](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/test_fwd.py#L87-L102) —— 金标 `fused_recurrent_kda`（fp64 逐 token 递推）就是这么调用的：吃激活后的 `g`/`beta`，因此 `A_log=None, dt_bias=None, lower_bound=None`、`use_gate_in_kernel=False`——**开关为 False ⟺ 激活外置**。

[tests/test_fwd.py:106-124](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/test_fwd.py#L106-L124) —— 同函数里对 `chunk_kda` 的调用。注意 L106-107 的注释与做法：*upstream hasn't implemented `use_beta_sigmoid_in_kernel`; pass post-sigmoid beta explicitly*——测试编写时上游还没有 `use_beta_sigmoid_in_kernel` 开关，所以这里传的是激活后的 beta（L107），且整个调用**没有**带该开关。对比 README/bench 的正式集成配置（带 `use_beta_sigmoid_in_kernel=True`、传 logits），可以看出这族开关是上游为承接 flashkda 后端而逐步补齐的：**每个 `use_*_in_kernel` 开关都把一项预处理从 Python 侧搬进 kernel，而 flashkda 的设计恰恰是三项全在 kernel 内做**（门控激活在 K1、L2 归一化在 K1、beta sigmoid 在 K2 Phase 2——分别见 u2-l7 与 u3-l4）。

[tests/test_fwd.py:129-141](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/test_fwd.py#L129-L141) —— `run_flash_kda_batched`：与上面 FLA 调用**同一组输入**直接调 `flash_kda.fwd` 的样本。两个细节值得注意：其一，`q.to(torch.bfloat16)`（L135）直接把 `torch.rand` 生成的**未归一化** q 喂入——印证 L2 归一化确实由 kernel 负责；其二，`h0.to(torch.bfloat16)`（L131）把 FLA 侧 fp32 的 `h0` 量化成 bf16 再传入——因为这个测试想对拍的是 bf16 状态路径。

[tests/test_fwd.py:197-209](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/test_fwd.py#L197-L209) —— `transpose_state_layout=True` 的布局证据：`r['tri_ht']`（FLA 侧最终状态）与 `r['final_state']`（flash_kda 侧最终状态）在 L203-205 **直接相减求误差**。两者能逐元素对上，说明开关为 True 时 FLA 输出的状态布局与 `flash_kda.fwd` 的 `[B,H,V,K]`（README L102-104）完全一致。开关为 False 时 FLA 的原生布局细节由上游定义，本仓库未涉及（待确认，可查上游源码）。

#### 4.1.4 代码实践

**实践：并排跑通两条入口，验证「分发命中的 chunk_kda ≈ 直接调 flash_kda.fwd」。**

1. **实践目标**：确认在同一组输入下，README 推荐配置的 `chunk_kda`（分发命中）与直接调用 `flash_kda.fwd` 产生一致量级的输出，从而验证参数映射表的正确性。
2. **操作步骤**：
   - 环境准备（SM90+ 机器，已完成 u1-l3 的构建安装）：
     ```bash
     pip install -U "flash-linear-attention>=0.5.0"
     ```
     这正是 [tests/test.sh:3](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/test.sh#L3) 做的事（该行还同时装了 matplotlib 供误差可视化）。
   - 编写 `two_entries.py`（示例代码）：
     ```python
     import logging, math, torch
     logging.basicConfig(level=logging.INFO)          # 观察分发日志（4.2 的主题）
     from fla.ops.kda import chunk_kda
     import flash_kda

     torch.manual_seed(0)
     B, T, H, D = 1, 2048, 8, 128
     scale = 1.0 / math.sqrt(D)
     q = torch.randn(B, T, H, D, dtype=torch.bfloat16, device='cuda')   # 未归一化
     k = torch.randn(B, T, H, D, dtype=torch.bfloat16, device='cuda')
     v = torch.randn(B, T, H, D, dtype=torch.bfloat16, device='cuda')
     g = torch.randn(B, T, H, D, dtype=torch.bfloat16, device='cuda')   # 激活前 logits
     beta = torch.randn(B, T, H, dtype=torch.bfloat16, device='cuda')   # 激活前 logits
     A_log = torch.rand(H, dtype=torch.float32, device='cuda')
     dt_bias = torch.rand(H, D, dtype=torch.float32, device='cuda')

     with torch.inference_mode():
         # 路径 A：FLA 分发入口（README L45-59 的配置）
         out_a, ht_a = chunk_kda(
             q=q, k=k, v=v, g=g, beta=beta, scale=scale,
             initial_state=None, output_final_state=True,
             use_gate_in_kernel=True, use_qk_l2norm_in_kernel=True,
             use_beta_sigmoid_in_kernel=True, safe_gate=True,
             A_log=A_log, dt_bias=dt_bias, lower_bound=-5.0,
             transpose_state_layout=True,
         )

     # 路径 B：直接调用后端（test_fwd.py run_flash_kda_batched 的写法）
     out_b = torch.zeros_like(q)
     ht_b = torch.zeros(B, H, D, D, dtype=torch.bfloat16, device='cuda')
     flash_kda.fwd(q, k, v, g, beta, scale, out_b,
                   A_log=A_log, dt_bias=dt_bias, lower_bound=-5.0,
                   final_state=ht_b)
     torch.cuda.synchronize()

     def err_ratio(x, y):
         return ((x.float() - y.float()).square().mean().sqrt()
                 / (y.float().square().mean().sqrt() + 1e-8)).item()
     print(f"out   err_ratio = {err_ratio(out_a, out_b):.3e}")
     print(f"state err_ratio = {err_ratio(ht_a.float(), ht_b.float()):.3e}")
     ```
   - 运行 `python two_entries.py`。
3. **需要观察的现象**：stderr 打出的 `[FLA Backend]` 日志行；两条路径输出/状态的 err_ratio 量级。
4. **预期结果**：日志显示命中 `-> flashkda`；err_ratio 为 0 或极小（同源自适应：分发后路径 A 最终也走到同一个 CUDA kernel，差异只可能来自 FLA 组装层的状态 dtype 选择等细节；具体数值**待本地验证**——`initial_state=None` 对应无状态输入，`output_final_state=True` 的回传 dtype 由上游组装逻辑决定）。
5. 若日志显示 `rejected: <reason>`，按 4.2 的方法排查（fla 版本、参数组合、硬件）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `flash_kda.fwd` 不能直接成为 FLA 的 drop-in 替换，而必须经过分发器组装？
**答案**：两者接口风格不同：`chunk_kda` 是函数式风格（返回 `(out, final_state)`、`initial_state` 可选传入、fp32 状态原生支持），而 `flash_kda.fwd` 要求调用方预分配 `out` 与 `final_state` 并以输出参数接收（u1-l5），运行模式（无状态/bf16/fp32）由「给了哪些状态张量及其 dtype」隐式决定（u2-l3）。分发器的职责就是补上这层组装：分配输出缓冲、按 `output_final_state` 决定是否传 `final_state`、按 `initial_state` 的 dtype 路由到对应的模板实例。

**练习 2**：`tests/test_fwd.py` 的 `run_fla_gold_reference` 中，为什么 `fused_recurrent_kda` 传 `use_gate_in_kernel=False` 且 `A_log=None`，而 `chunk_kda` 传 `use_gate_in_kernel=True` 且带上 `A_log`/`dt_bias`？
**答案**：金标走「激活外置」：L78-80 已在 Python 侧用 fp64 算好激活后的门控 `g_activated`，kernel 只需吃现成数值，故开关为 False、门控参数传 None；`chunk_kda` 走「激活内置」（与 flashkda 的 K1 设计一致），传原始 logits 由 kernel 内激活。开关族的语义就是「这项预处理在不在 kernel 里做」。

**练习 3**：如果用户在调用 `chunk_kda` 前自己做了 `q = F.normalize(q, p=2, dim=-1)`，又开了 `use_qk_l2norm_in_kernel=True`，会发生什么？
**答案**：仍然正确。kernel 内的 L2 归一化（u2-l7 模块①）对已归一化的输入近似是恒等操作（范数≈1），仓库自己的测试与 bench 也常见这种「预归一化 + kernel 内再归一化」的叠加输入（如 [tests/test_fwd.py:230-231](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/test_fwd.py#L230-L231)、[benchmarks/bench_fwd.py:53-54](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/benchmarks/bench_fwd.py#L53-L54)）；而 [tests/test_fwd.py:135](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/test_fwd.py#L135) 则直接喂未归一化的 q，同样正确。契约只要求「不必预归一化」，不禁止。

### 4.2 环境开关与日志调试

#### 4.2.1 概念说明

分发机制一旦上线，「我的调用到底走了哪个后端」就成了黑盒。FLA 提供了两个观测/控制接口（均记载于 README）：

- **日志（观测）**：`logging.basicConfig(level=logging.INFO)` 后，每次 `chunk_kda` 调用会打出一行 INFO 日志——命中时是 `[FLA Backend] kda.chunk_kda -> flashkda`，未命中时是 `... rejected: <reason>`，把拒绝原因直接告诉用户。
- **环境变量（控制）**：`FLA_FLASH_KDA=0` 强制跳过 flashkda 后端，回到 Triton 路径。

一个合格的集成工作流应该默认打开这两样：日志让你确认命中，开关让你随时能退。这对三类人各有用途——部署者（确认加速生效）、测试者（A/B 对拍）、排障者（怀疑新后端有问题时一键回退）。

#### 4.2.2 核心流程

用日志与开关做分发诊断的流程：

```text
1. logging.basicConfig(level=logging.INFO)   # 必须在调用前配置
2. 运行一次 chunk_kda，收集 stderr 的 [FLA Backend] 行
3. 分支：
   a. "-> flashkda"        → 命中；如需对照，shell 层面设 FLA_FLASH_KDA=0 重跑
   b. "rejected: <reason>" → 按 reason 修参数/环境（版本、开关、硬件）
4. 注意：FLA_FLASH_KDA 是进程启动时读取的环境变量，
   应写成 `FLA_FLASH_KDA=0 python xxx.py`，
   不要依赖进程内 os.environ 修改后再触发（读取时机属上游实现，待确认）
```

#### 4.2.3 源码精读

**① 开关与日志的一手定义。**

[README.md:62-64](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/README.md#L62-L64) —— 两行的分量：`FLA_FLASH_KDA=0` 是「opt out（主动退出）」，说明默认值是启用（非 0）；日志格式串 `[FLA Backend] kda.chunk_kda -> flashkda` 中的 `kda.chunk_kda` 是 FLA 内部对该算子的标识、`flashkda` 是后端名，`rejected: <reason>` 承诺给出具体拒绝原因。日志的措辞、拒绝条件的完整清单在上游 FLA 源码中，本仓库不重复定义。

**② 测试环境默认具备分发能力。**

[tests/test.sh:1-4](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/test.sh#L1-L4) —— 一键测试三步：可编辑安装 flash_kda（L2）、安装 `flash-linear-attention>=0.5.0` 与 matplotlib（L3）、跑 `tests/test_fwd.py`（L4）。也就是说，**凡是完整跑过 test.sh 的环境，FLA 分发都是默认开启的**——这既是本讲实践的前提，也埋着下一个推论。

**③ 重要推论：bench 的 `chunk_kda` 基线可能已经不是 Triton。**

[benchmarks/bench_fwd.py:95-114](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/benchmarks/bench_fwd.py#L95-L114) —— 结合 4.1.3 ③ 的事实（`run_chunk_kda` 的参数与 README 命中配置完全一致）与 ② 的事实（装了 fla≥0.5.0 的环境默认启用分发），得到一个容易被忽略的结论：**在默认环境变量下运行 `bench_fwd.py`，其 `chunk_kda` 一栏测到的可能已经是 flashkda 后端而非 Triton**——那么这栏数字与 `flash_kda (fp32 state)` 一栏的差别只剩「FLA 组装层的开销」。要在基准中测到真正的 Triton 基线，必须以 `FLA_FLASH_KDA=0` 运行。这直接修正了对 u3-l10 性能报告的解读姿势：报告中的 chunk_kda 对照数据在什么环境变量下采集，决定了它测的是谁。

同理可提出一个值得动手验证的问题：[tests/test_fwd.py:108-124](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/test_fwd.py#L108-L124) 中 `run_fla_gold_reference` 对 `chunk_kda` 的调用**没有带** `use_beta_sigmoid_in_kernel=True`（历史原因，见 4.1.3 ④），这组参数是否仍满足分发条件、`chunk_o`/`chunk_ht` 到底由谁算出——用 4.2.4 的日志实践可以直接观察到答案（**待本地验证**）。

#### 4.2.4 代码实践

**实践：用日志画出一张「配置 → 命中/拒绝」表。**

1. **实践目标**：亲手触发命中与未命中两种日志，整理出（在你的环境里）分发条件的经验边界。
2. **操作步骤**：
   - 以 4.1.4 的 `two_entries.py` 为底本（保留 `logging.basicConfig(level=logging.INFO)`），做三次运行：
     ```bash
     python two_entries.py                          # 运行 1：完整命中配置
     FLA_FLASH_KDA=0 python two_entries.py          # 运行 2：强制退出
     ```
     再编辑脚本做运行 3：注释掉 `use_beta_sigmoid_in_kernel=True`（其余不动）。
   - 每次运行记录 stderr 中 `[FLA Backend]` 行。
3. **需要观察的现象**：三次运行各自的日志措辞——运行 1 应为 `-> flashkda`；运行 2 应为 `-> triton` 或等价的回退措辞（**具体措辞待本地验证**，README 只承诺运行 1 的格式）；运行 3 是本实践的关键观察点。
4. **预期结果**：得到一张三行小表「配置 → 日志 → 实际后端」。若运行 3 显示 `rejected`，说明该开关是命中条件之一；若仍命中，说明它不是（或不是唯一组合条件）。无论哪种结果，你都拿到了第一手的分发边界证据——这比记住任何文档结论都可靠。
5. 附加观察：在 `torch.inference_mode()` **外**调用（去掉 with）再看一次日志，检验推理模式是否为命中条件之一（README 将其列为使用要求，但是否硬性拒绝属上游实现，**待本地验证**）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `FLA_FLASH_KDA=0` 要在 shell 层面设置，而不是在 Python 里 `os.environ['FLA_FLASH_KDA']='0'` 之后调用？
**答案**：环境变量通常在进程/模块初始化时读取一次（读取时机属上游实现细节，待确认）。在 shell 层面 `FLA_FLASH_KDA=0 python xxx.py` 保证进程一启动变量就在场，不依赖任何读取时机假设；进程内修改则可能因为库已经读过而不生效。两种方式都试一次、用日志验证，是最稳妥的做法。

**练习 2**：生产环境里用户反馈「装了 FlashKDA 之后某个长尾 case 结果异常」，你如何在不卸载包的情况下恢复服务？
**答案**：给服务进程加环境变量 `FLA_FLASH_KDA=0` 重启——分发器跳过 flashkda，回到 FLA 原 Triton 路径（README L62 的 opt out 语义）。包不用卸载，回退是一行配置的事；之后再开 `logging.INFO` 复现该 case 的命中日志，把问题定位到具体后端。

**练习 3**：4.2.3 ③ 推论「bench 的 chunk_kda 一栏测到的可能已是 flashkda」，如何用实验证实或证伪？
**答案**：分两次跑同一 bench：`python benchmarks/bench_fwd.py` 与 `FLA_FLASH_KDA=0 python benchmarks/bench_fwd.py`（可只跑一个形状控制变量）。若两次的 chunk_kda 耗时显著不同（且开日志时分别显示 `-> flashkda` 与回退），则证实默认环境测到的是 flashkda 自身；若几乎相同，则说明该环境下分发未命中（例如硬件或版本不满足），基线仍是 Triton。

### 4.3 Triton 回退对比

#### 4.3.1 概念说明

有了退出开关，flashkda 与 Triton 两条路径就可以在**完全相同的输入**下正面对比。对比有两个维度：

- **数值一致性**：两个 kernel 的数值方案不同（Triton chunk_kda 是 FLA 自己的精度取舍；flashkda 是 u3-l8 的 bf16/fp32 分工），输出不 bit-exact 是预期内的，比较对象是**相对误差量级**，且金标应取第三方——仓库的做法是用 fp64 的 `fused_recurrent_kda` 逐 token 递推当裁判（u3-l9 的「金标」策略），两个 chunk 实现都与金标比，而不是互相比对裁判。
- **性能**：用 `bench_fn` 的 cuda.Event 计时口径（u3-l10），注意对比时必须固定后端（默认 vs `FLA_FLASH_KDA=0`），否则测的是「组装层开销」而非「kernel 差距」。

这一模块的方法学其实全部来自 `tests/test_fwd.py` 的两个 `*_vs_fla` 测试——它们是仓库作者自己做的「flashkda vs FLA」对比实验的存档。

#### 4.3.2 核心流程

一次完整的两路径对比实验：

```text
1. 构造输入（g/beta 传 logits；q/k 不预归一化；准备 cu_seqlens 与逐序列初始状态）
2. 计算金标 tri, tri_ht = fused_recurrent_kda(fp64, 激活外置)      # 裁判
3. 路径 F（flashkda）：直接调 flash_kda.fwd（或经分发命中的 chunk_kda）
4. 路径 T（Triton）：FLA_FLASH_KDA=0 环境下调 chunk_kda（同参数）
5. 一致性：err_ratio(tri, out_F) vs err_ratio(tri, out_T)；状态同理（容差放宽）
6. 性能：两条路径各自 bench_fn(fn, warmup, iters, repeats) → mean/min/max
7. 汇总成表：后端 × {out err, state err, mean ms} 
```

#### 4.3.3 源码精读

**① 对拍脚手架：金标 + 两个被测。**

[tests/test_fwd.py:71-126](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/test_fwd.py#L71-L126) —— `run_fla_gold_reference` 一次返回四个量：fp64 金标 `(tri, tri_ht)` 与 chunk 版 `(chunk_o, chunk_ht)`。注意 L85 的 `fla_kwargs = dict(cu_seqlens=cu_seqlens) if cu_seqlens is not None else {}`——同一个函数同时服务 batched 与 varlen 两个测试（L71 的签名带 `cu_seqlens=None` 默认值），varlen 时两个 FLA 算子都收到同一份 `cu_seqlens`。

**② 误差度量与断言分层。**

[tests/test_fwd.py:13-16](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/test_fwd.py#L13-L16) —— `get_err_ratio`：相对 RMSE \[ \text{err\_ratio} = \frac{\|x-y\|_2}{\|y\|_2} \]，u3-l9 介绍过的窗口化统计（L28-38 `collect_windowed_errors`）在此基础上按 token 位置切片，用于观察误差是否随序列推进累积。

[tests/test_fwd.py:345-346](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/test_fwd.py#L345-L346) —— 测试主体里 flashkda 与 chunk_kda 以**完全对称**的方式与金标对拍并打印 err_ratio——这正是本模块要复刻的对比姿势。

[tests/test_fwd.py:360-362](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/test_fwd.py#L360-L362) —— 断言分层：输出 `o` 是硬断言（容差 0.005），最终状态 `ht` 用 `warning=True` 降级、chunk_kda 的状态同样只警告——状态是长程累积量，误差天然更大（u3-l9 的结论）。做你自己的对比实验时应沿用这一分层直觉，而不是对 out 和 state 用同一把尺子。

**③ inference_mode 与测试的组织方式。**

[tests/test_fwd.py:315-316](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/test_fwd.py#L315-L316) 与 [tests/test_fwd.py:366-367](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/test_fwd.py#L366-L367) —— `test_fwd_vs_fla` / `test_fwd_varlen_vs_fla` 两个测试都以 `@torch.inference_mode()` 装饰，varlen 版用 `seq_lens = [1300, 547, 2048, 963, 271, 3063]`（L376）构造 6 条变长序列。你的对比脚本应以它们为模板。

**④ 性能对比的计时口径。**

[benchmarks/bench_fwd.py:8-30](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/benchmarks/bench_fwd.py#L8-L30) —— `bench_fn`：热身后按 repeats 轮、每轮 iters 次连续入队 cuda.Event 计时，返回 mean/min/max（u3-l10 已精读）。对比两条后端时直接复用它，保证与官方报告同口径。

#### 4.3.4 代码实践

**实践：量化「回退 Triton」的代价——一次一致性 + 性能的双维对比。**

1. **实践目标**：在同一组 varlen 输入上，量化 flashkada 与 Triton 两条路径相对 fp64 金标的误差量级差与耗时差。
2. **操作步骤**：直接综合实践（第 5 节）的 `fla_dispatch.py` 已覆盖本实践，此处给出聚焦性能的最小变体——把两条路径各自包成闭包喂给 `bench_fn`（示例代码）：
   ```python
   from benchmarks.bench_fwd import bench_fn   # 若不在包内，可把 L8-30 复制进脚本
   # run_triton() / run_flash() 的构造方式见第 5 节
   print("flashkda :", bench_fn(run_flash,  warmup=10, iters=50, repeats=5))
   print("triton   :", bench_fn(run_triton, warmup=10, iters=50, repeats=5))
   ```
   运行两次（默认与 `FLA_FLASH_KDA=0`），或在一个脚本内只调 `chunk_kda`、靠两次进程切换后端。
3. **需要观察的现象**：两条路径的 mean/min/max 三列耗时；err_ratio 两列。
4. **预期结果**：在 H20 级别的卡上，varlen 场景 flashkda 相对 Triton chunk_kda 应有约 1.85×–2.31× 的端到端加速（u1-l1 引用的 BENCHMARK_H20 结论）；两条路径相对金标的输出 err_ratio 应在同一量级（都是 bf16 计算路径）。具体数字**待本地验证**。
5. 若两条路径耗时不差分毫，先回 4.2 检查后端是否真的切换了（看日志）。

#### 4.3.5 小练习与答案

**练习 1**：为什么对比的裁判要用 `fused_recurrent_kda`（fp64、逐 token）而不是让 flashkda 与 chunk_kda 互相比？
**答案**：两个 chunk 实现的数值方案不同，互比只能得到「差异」，无法判断谁更接近真值；fp64 逐 token 递推无分块近似、无双精度不足问题，是独立的真值来源（u3-l9 的金标选择理由）。互比的问题在于误差来源不可归因——是舍入不同还是某一方真错了，说不清。

**练习 2**：对比实验里状态（`ht`）的容差为什么要比输出（`o`）宽松，甚至测试里只给 warning？
**答案**：状态是沿整个序列累积的量，误差单调堆积；输出只受当前 tile 内状态影响。仓库测试的断言分层（L360-362：`o` 硬断言、`ht` warning=True）正反映了这一统计特性。做回归对比时对状态用同一把宽松尺子，可以避免长序列下的误报。

**练习 3**：`run_fla_gold_reference` 里金标路径把 `transpose_state_layout=True` 也传给了 `fused_recurrent_kda`（L100），为什么？
**答案**：为了布局对齐——金标的状态要与 flashkda 的 `[B,H,V,K]`（以及同样开了该开关的 chunk_kda 状态）直接相减（L203-205），三个实现必须在同一布局下比较。开关保证 FLA 的两个算子都把状态「转置」到与后端一致的布局上，省掉测试侧手工转置。

## 5. 综合实践

**任务：编写 `fla_dispatch.py`，完整走一遍「分发命中 → 日志确认 → 强制回退 → 双维对比」的闭环。** 这是本讲的实践主任务，综合了三个模块的全部要点。

```python
# fla_dispatch.py（示例代码：以 README.md L45-59 与 tests/test_fwd.py 为模板）
import logging, math, sys, torch

logging.basicConfig(level=logging.INFO)          # 模块二：让 [FLA Backend] 日志可见
from fla.ops.kda import chunk_kda, fused_recurrent_kda

H, D = 8, 128
seq_lens = [1300, 547, 2048]                     # varlen：含两个非 16 倍数的尾块
T_total, N = sum(seq_lens), len(seq_lens)
cu_seqlens = torch.tensor([0] + list(torch.cumsum(torch.tensor(seq_lens), 0)),
                          dtype=torch.long, device='cuda')

torch.manual_seed(42)
q = torch.rand(1, T_total, H, D, dtype=torch.bfloat16, device='cuda')   # 不预归一化
k = torch.rand(1, T_total, H, D, dtype=torch.bfloat16, device='cuda')
v = torch.rand(1, T_total, H, D, dtype=torch.bfloat16, device='cuda')
g = torch.randn(1, T_total, H, D, dtype=torch.bfloat16, device='cuda')  # logits
beta = torch.randn(1, T_total, H, dtype=torch.bfloat16, device='cuda')  # logits
A_log = torch.zeros(H, dtype=torch.float32, device='cuda')
dt_bias = torch.zeros(H, D, dtype=torch.float32, device='cuda')
h0 = torch.randn(N, H, D, D, dtype=torch.float32, device='cuda')        # 逐序列初始状态
scale, LOWER_BOUND = 1.0 / math.sqrt(D), -5.0

with torch.inference_mode():
    out, ht = chunk_kda(                          # 模块一：README 的命中配置
        q=q, k=k, v=v, g=g, beta=beta, scale=scale,
        initial_state=h0, output_final_state=True,
        use_gate_in_kernel=True, use_qk_l2norm_in_kernel=True,
        use_beta_sigmoid_in_kernel=True, safe_gate=True,
        A_log=A_log, dt_bias=dt_bias, lower_bound=LOWER_BOUND,
        transpose_state_layout=True, cu_seqlens=cu_seqlens,
    )

    # 金标（激活外置的 fp64 逐 token 递推，模板：tests/test_fwd.py L78-102）
    g_act = LOWER_BOUND * torch.sigmoid(torch.exp(A_log.double().view(1,1,H,1))
                                        * (g.double() + dt_bias.double()))
    tri, tri_ht = fused_recurrent_kda(
        q=q.double(), k=k.double(), v=v.double(), g=g_act,
        beta=torch.sigmoid(beta.double()),
        A_log=None, dt_bias=None, scale=scale,
        initial_state=h0.double(), output_final_state=True,
        use_qk_l2norm_in_kernel=True, use_gate_in_kernel=False,
        lower_bound=None, transpose_state_layout=True, cu_seqlens=cu_seqlens,
    )

def err_ratio(x, y):
    return ((x.float()-y.float()).square().mean().sqrt()
            / (y.float().square().mean().sqrt()+1e-8)).item()

tag = sys.argv[1] if len(sys.argv) > 1 else "default"
print(f"[{tag}] out   err_ratio vs fp64 gold: {err_ratio(out, tri.float()):.4e}")
print(f"[{tag}] state err_ratio vs fp64 gold: {err_ratio(ht.float(), tri_ht.float()):.4e}")

def timed(fn, iters=50):
    for _ in range(10): fn()
    torch.cuda.synchronize()
    s, e = torch.cuda.Event(True), torch.cuda.Event(True)
    s.record()
    for _ in range(iters): fn()
    e.record(); torch.cuda.synchronize()
    return s.elapsed_time(e) / iters

with torch.inference_mode():
    def run():
        chunk_kda(q=q, k=k, v=v, g=g, beta=beta, scale=scale,
                  initial_state=h0, output_final_state=True,
                  use_gate_in_kernel=True, use_qk_l2norm_in_kernel=True,
                  use_beta_sigmoid_in_kernel=True, safe_gate=True,
                  A_log=A_log, dt_bias=dt_bias, lower_bound=LOWER_BOUND,
                  transpose_state_layout=True, cu_seqlens=cu_seqlens)
    print(f"[{tag}] mean latency: {timed(run):.3f} ms")
```

**执行与验收**：

1. **准备**：SM90+ 机器；完成 u1-l3 构建安装；`pip install -U "flash-linear-attention>=0.5.0"`（即 [tests/test.sh:3](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/test.sh#L3) 的动作）。
2. **两轮运行**：
   ```bash
   python fla_dispatch.py flashkda                    # 轮 1：默认（预期命中分发）
   FLA_FLASH_KDA=0 python fla_dispatch.py triton      # 轮 2：强制回退
   ```
3. **检查点**（预期结果，具体数值待本地验证）：
   - 轮 1 stderr 出现 `[FLA Backend] kda.chunk_kda -> flashkda`；轮 2 出现回退（措辞待验证）。
   - 两轮的 err_ratio 都在 1e-2 量级以内（bf16 路径 vs fp64 金标的正常水平；状态容差直觉见练习 2）。
   - 轮 1 的 mean latency 显著低于轮 2（H20 上 varlen 预期约 1.85×–2.31×，u1-l1）。
   - 若两轮结果完全相同（含日志），说明轮 2 的环境变量未生效——回到 4.2 练习 1 排查。
4. **整理产出**：一张 4 列小表（后端 / out err / state err / mean ms）+ 两行日志摘录。这张表就是你对「FlashKDA 作为 FLA 后端」的完整验收记录。

## 6. 本讲小结

- FlashKDA 的生态位是 **FLA `chunk_kda` 的 drop-in 后端**：分发器实现在上游（fla-org#852），本仓库的职责是让 `flash_kda.fwd` 满足后端契约——g/beta 传激活前 logits、q/k 不预归一化、状态布局 `[B,H,V,K]`、`K=V=128`。
- 契约的映射语言是 FLA 侧的 `use_*_in_kernel` 开关族：每个开关把一项预处理（门控激活 / L2 归一化 / beta sigmoid）从 Python 侧搬进 kernel，与 flashkda「K1 做激活与归一化、K2 做 beta sigmoid」的内聚设计一一对应；`transpose_state_layout=True` 对齐状态布局。
- 两个运维接口：`logging.basicConfig(level=logging.INFO)` 观察 `[FLA Backend] kda.chunk_kda -> flashkda` / `rejected: <reason>`；`FLA_FLASH_KDA=0`（shell 层面设置）强制回退 Triton。
- 一个反直觉推论：bench 与测试环境默认装了 fla≥0.5.0，而 [benchmarks/bench_fwd.py:98-111](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/benchmarks/bench_fwd.py#L98-L111) 的 chunk_kda 对照配置恰是命中配置——**默认环境下基准里的「chunk_kda 基线」可能已是被分发的 flashkda**，测真 Triton 基线必须带 `FLA_FLASH_KDA=0`。
- 对比方法学：以 fp64 `fused_recurrent_kda` 为金标、输出硬断言而状态放宽、`bench_fn` 的 cuda.Event 口径计时——全部取自 `tests/test_fwd.py` 的既有实践。

## 7. 下一步学习建议

本讲是单元三的倒数第二讲，FLA 集成把前面所有内核知识（u2/u3 的 K1、K2、精度、测试、基准）接到了真实生态入口上。接下来：

- **u3-l12（二次开发实践：消融、调参与扩展方向）**：本讲的对比流程（改一处 → 重编译 → 回归 → benchmark）正是二次开发工作流的验收半边；u3-l12 把另一半（编译期消融开关、调参旋钮、扩展 head_dim 的影响面分析）补齐，建议把本讲的 `fla_dispatch.py` 留着，作为 u3-l12 消融实验的标准测试载荷。
- 若想深挖分发器本身：读上游 [fla-org/flash-linear-attention#852](https://github.com/fla-org/flash-linear-attention/pull/852) 的实现——重点找拒绝条件的完整清单（本讲标注「待确认」的三处：`safe_gate` 语义、`FLA_FLASH_KDA` 读取时机、inference_mode 是否硬性条件——都能在那里找到答案）。
- 若关注「为什么 varlen 下加速比更高」：回到 [BENCHMARK_H20.md](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/BENCHMARK_H20.md) 与 u3-l7（尾块逐元素写避免 TMA 越界）对照阅读——变长序列的正确性与性能都系于尾块处理。
