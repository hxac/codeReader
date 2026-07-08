# 正确性测试与 monkey-patch 测试

## 1. 本讲目标

本讲是「测试、集成与扩展」单元的第一篇，聚焦 FFPA 仓库里三个最贴近用户的测试文件：

- `tests/test_ffpa_fwd.py`——前向正确性与分发冒烟测试；
- `tests/test_ffpa_bwd.py`——反向梯度正确性测试；
- `tests/test_monkey_patch.py`——锁定「一行 monkey-patch 替换 SDPA」这个公开用法不回归。

读完本讲，你应当能够：

1. 说出 `CORRECTNESS_SHAPES` 与 `DISPATCH_SHAPES` 这两套形状集合各自的覆盖目标（精度 vs. 能跑通）；
2. 解释 `_sdpa_ref` 与 `_sdpa_fallback` 两个参考实现的差别，以及它们分别对应 FFPA 的哪条运行时路径；
3. 理解 `IS_ROCM` 检测为何会改变 dropout 测试的命运，以及测试如何用容差放宽 / `xfail` 来吸收 ROCm 的数值差异；
4. 复述 `test_monkey_patch.py` 如何用「把原生 SDPA 换成抛异常的桩」这一招，同时锁定「大 D 走 FFPA」与「小 D 回退不递归」两件事。

本讲是高级（advanced）内容，默认你已经读过前置讲义：知道 `ffpa_attn_func` 的签名与 `[B,Nh,N,D]` 布局（u2-l1）、知道前向/反向分发到 aten/cuda/triton/cutedsl 四后端的机制（u3-4），以及 monkey-patch 的基本动机（u1-l4）。

## 2. 前置知识

- **参考实现（reference）**：在数值测试里，「参考实现」指的是被公认正确、用来给被测对象当标尺的另一份实现。FFPA 的参考实现就是 PyTorch 自带的 SDPA（`F.scaled_dot_product_attention`）。所有 FFPA kernel 都在和 SDPA比误差。
- **容差（tolerance）**：浮点数比较几乎不可能「完全相等」，所以测试用「绝对误差 atol + 相对误差 rtol」给一个允许的偏差范围。`torch.testing.assert_close(a, b, atol=, rtol=)` 当 \(|a-b| \le \text{atol} + \text{rtol}\cdot|b|\) 时判通过。
- **fp16 / bf16**：两种 16 位浮点。fp16 尾数 10 位、bf16 尾数 7 位，所以 bf16 精度更差、需要更宽的容差。
- **monkey-patch**：在运行时把某个模块的函数替换成自己的实现。FFPA 的「一行接入」就是 `F.scaled_dot_product_attention = ffpa_attn_func`。
- **`torch._C._nn.scaled_dot_product_attention`**：SDPA 的底层 C++ 绑定。它和 Python 层的符号 `F.scaled_dot_product_attention` 是「同一个算子的两个名字」。一旦你 monkey-patch 了 Python 符号，回退路径就**必须**调底层绑定，否则会无限递归——这是本讲的核心悬念。
- **ROCm / Triton-AMD**：ROCm 是 AMD GPU 的软件栈，Triton 可以编译成 HIP 代码跑在 AMD 卡上。FFPA 同时支持 NVIDIA（CUDA/Triton/CuTeDSL）与 AMD（Triton-AMD）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tests/test_ffpa_fwd.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_ffpa_fwd.py) | 前向正确性矩阵 + 分发冒烟 + 各特性（mask/dropout/GQA/causal/cross/decode）对 SDPA 的逐项校验。本讲主角之一。 |
| [tests/test_ffpa_bwd.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_ffpa_bwd.py) | 反向梯度 dQ/dK/dV（部分用例含 dMask）对 SDPA 的校验，含 SM90 TMA/persist/split-launch 开关、decode 反向、dropout 反向。本讲重点取其容差与 ROCm 处理。 |
| [tests/test_monkey_patch.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_monkey_patch.py) | 锁定 monkey-patch 公开用法：大 D 必须走 FFPA、小 D 回退必须走原生 SDPA 且不递归。本讲另一主角。 |
| [src/ffpa_attn/ffpa_attn_interface.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ffpa_attn_interface.py) | `ffpa_attn_func` 的真实实现。测试锁定的「回退调底层绑定」那条 `# HACK` 分支就在这里。 |
| [src/ffpa_attn/functional.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py) | `FFPAAttnMeta.fallback()` 的真实回退判定逻辑。测试里的 `_is_sdpa_fallback_shape` 是它的镜像。 |

---

## 4. 核心概念与源码讲解

### 4.1 正确性测试的整体设计：CORRECTNESS_SHAPES 与 DISPATCH_SHAPES

#### 4.1.1 概念说明

FFPA 的前向测试把「形状空间」拆成了**两层不同强度的覆盖**，这是整个测试套件最值得学习的工程手法：

- **正确性层（accuracy）**：用少量、精心挑选的形状，把 FFPA 输出和 SDPA 参考逐元素比误差（`assert_close`）。形状少是为了让它在 CI 里跑得快（注释目标 <~25 s on L20）。
- **分发层（dispatch / smoke）**：用大批量形状把「每一个 `(head_num, head_dim)` 组合」都启动一次 kernel，**只检查输出形状对、dtype 对、不含 NaN/Inf**，不比精度。它要回答的问题是「这条 tile 配置能不能正常 launch、不崩」。

两层分离的好处：正确性测试慢（要跑参考实现、要逐元素比较），不可能覆盖笛卡尔积；分发测试快（只看 finiteness），可以铺得很密。两者各司其职。

还有一个常被忽略的细节：**小 D（D≤256）在默认配置下根本不跑 FFPA kernel，而是回退到 SDPA**（见 u3-l3 的 `fallback()`）。所以「正确性层」里那些 D=64/128 的形状，实际上校验的是**回退路径**——即「FFPA 的回退结果 == 原生 SDPA」；而 D=320/512/640 的形状才真正校验 **FFPA 自己的 kernel**。一套矩阵同时覆盖了两条路径。

#### 4.1.2 核心流程

前向测试文件顶部定义了四个「形状维度」常量，再由它们派生出两个形状集合：

```
SEQLENS  = [1024, 4096, 8192]
HEADDIMS = [64, 128, 320, 512, 640]   # 含小 D(64/128) 与大 D(320+)
HEADNUMS = [8, 16, 32, 48]
DTYPES   = [fp16, bf16]

CORRECTNESS_SHAPES = 7 条手工挑的代表性形状
DISPATCH_SHAPES    = HEADNUMS × HEADDIMS 全笛卡尔积（在 N=1024）
```

- `CORRECTNESS_SHAPES` 注释明确写道：「每个 (dtype, headdim) 类别取一个代表形状，外加两个更长 seqlen 的抽查」，目标是「快、同时覆盖 small_d 与 large_d 两条路径」。
- `DISPATCH_SHAPES` 用 `itertools.product` 一行生成，注释写「在 N=1024 下每个 (H,D) 都启动一次，验证 tile launch + 输出有限；精度另测」。

#### 4.1.3 源码精读

正确性形状集合（7 条，覆盖 D=64/128/320/512/640 五个头维，外加两条 4096 长序列抽查）:

[tests/test_ffpa_fwd.py:32-40](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_ffpa_fwd.py#L32-L40) —— 注释与 7 条 `(B,H,N,D)` 形状。

分发形状集合（HEADNUMS × HEADDIMS 笛卡尔积，全部固定 N=1024）:

[tests/test_ffpa_fwd.py:44-45](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_ffpa_fwd.py#L44-L45) —— 用 `itertools.product` 生成全部 `(1,H,1024,D)`。

两个测试函数分别消费这两套形状：

[tests/test_ffpa_fwd.py:140-149](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_ffpa_fwd.py#L140-L149) —— `test_ffpa_attn_func_matches_sdpa`，正确性层：调 `ffpa_attn_func` 与 `_sdpa_ref`，断言 dtype/shape 一致、输出 finite、`assert_close` 比误差。

[tests/test_ffpa_fwd.py:152-159](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_ffpa_fwd.py#L152-L159) —— `test_ffpa_attn_func_dispatch_shapes`，分发层：**只**断言 shape、dtype、`isfinite`，不比精度。注意它没有任何参考实现。

对照这两段就能看出「精度 vs. 能跑通」的分野：前者 `assert_close(out, ref, ...)`，后者根本没有 `ref`。

> 生产侧对照：分发测试里那些小 D 形状会触发 `fallback()` 的真实回退。`fallback()` 在 [src/ffpa_attn/functional.py:515-522](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L515-L522) 用一个 `any([...])` 列出全部回退条件（含 `_should_use_aten_small_d_forward`、`D > 1024`、`8 <= Nq < 512`、`Nkv < 512`）。

#### 4.1.4 代码实践

**实践目标**：亲手验证「`-k 512` 选出的子集，既包含走 FFPA kernel 的大 D 形状，也包含走回退的小 D 形状」，并读懂一个正确性断言。

**操作步骤**：

1. 在仓库根目录执行（需要 CUDA 环境；无 GPU 则步骤 1 标「待本地验证」）：

   ```bash
   pytest tests/test_ffpa_fwd.py -k '512' -v
   ```

2. 打开被选中的 `test_ffpa_attn_func_matches_sdpa[fp16-1-16-1024-512]`，对照源码 [tests/test_ffpa_fwd.py:142-149](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_ffpa_fwd.py#L142-L149) 阅读四条断言：`out.dtype == dtype`、`out.shape == ref.shape`、`isfinite(out).all()`、`assert_close(out, ref, **_tolerance(dtype))`。

**需要观察的现象**：

- `-k '512'` 会命中所有 id 字符串里含 `512` 的用例（包括 `CORRECTNESS_SHAPES` 里 `(1,16,1024,512)` 与 CUDA mask 测试 `D=512` 等）。
- D=512 这条既不在 `D>1024` 也不在 `8<=Nq<512`（Nq=1024），故 `fallback()` 返回 False，**真的进了 FFPA kernel**。

**预期结果**：D=512 的用例通过，误差落在 fp16 容差（atol=rtol=1e-2）内。若无 GPU，标记「待本地验证」，但你应能口头解释每条断言的含义。

#### 4.1.5 小练习与答案

**练习 1**：`DISPATCH_SHAPES` 共有多少条形状？为什么它不调用任何参考实现？

**答案**：`len(HEADNUMS) × len(HEADDIMS) = 4 × 5 = 20` 条。它只验证「kernel 能 launch、输出 shape/dtype 对、不含 NaN/Inf」，不验证精度，所以不需要参考实现——精度由 `CORRECTNESS_SHAPES` 那一层负责。

**练习 2**：`CORRECTNESS_SHAPES` 里的 `(1,8,1024,64)`（D=64）实际校验的是哪条运行时路径？为什么？

**答案**：校验的是**回退路径**。因为默认 triton 后端下，D=64≤256 触发 `_should_use_aten_small_d_forward` 为真，`fallback()` 返回 True，`ffpa_attn_func` 直接调原生 SDPA。所以这条用例本质是在验证「FFPA 的回退结果 == SDPA 参考」。

---

### 4.2 参考实现：_sdpa_ref 与 _sdpa_fallback

#### 4.2.1 概念说明

测试里有两个长得几乎一样的「SDPA 参考」函数，但它们指向**不同的运行时入口**，混用会让测试结果失真：

- `_sdpa_ref`：直接调 Python 层的 `F.scaled_dot_product_attention`。
- `_sdpa_fallback`：调**底层 C++ 绑定** `torch._C._nn.scaled_dot_product_attention`，并刻意把 `dropout_p`/`is_causal`/`enable_gqa` 等关键字原样透传，注释明说它是「镜像公开 API 的原始 SDPA 回退路径，不重写用户输入」。

为什么要有两个？因为 FFPA 内部那条回退分支调的正是底层绑定（为了避免 monkey-patch 递归）。当测试想「模拟 FFPA 自己的回退会算出什么」时，必须用 `_sdpa_fallback` 而不是 `_sdpa_ref`，否则两者用的是同一个 Python 符号，等于没在测分发逻辑。

> 还有一个测试专用的辅助 `_is_sdpa_fallback_shape`，它把生产侧 `fallback()` 的判定逻辑**复制了一份**，用来在测试里预先判断「这个形状会不会回退」。如果会回退，测试就用 `_sdpa_fallback` 当参考；如果不会，就用 `_sdpa_ref`（或构造显式 `attn_mask`）。

#### 4.2.2 核心流程

```
测试拿到一个形状 (Nq, Nkv, D, ...)
   │
   ├─ _is_sdpa_fallback_shape(q, k, forward_backend=...) == True ?
   │     是 → ref = _sdpa_fallback(...)        # 镜像 FFPA 的回退分支
   │     否 →
   │        ├─ causal 且 Nq != Nkv → 手工构造尾对齐 attn_mask，调 F.sdpa
   │        └─ 否                   → ref = _sdpa_ref(...)      # 普通 SDPA 参考
   │
   └─ assert_close(ffpa_out, ref, **_tolerance(dtype))
```

关键点：参考实现的选择**取决于被测形状会不会回退**，这保证无论 FFPA 走 kernel 还是回退，参考都和它「同一条语义路径」对齐。

#### 4.2.3 源码精读

普通参考实现，调 Python 层 `F.scaled_dot_product_attention`，固定 `scale=1/sqrt(D)`：

[tests/test_ffpa_fwd.py:48-51](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_ffpa_fwd.py#L48-L51) —— `_sdpa_ref`。

镜像回退路径的参考实现，调**底层绑定** `torch._C._nn.scaled_dot_product_attention`，docstring 写明「Mirror the public raw SDPA fallback path without rewriting user inputs」：

[tests/test_ffpa_fwd.py:54-75](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_ffpa_fwd.py#L54-L75) —— `_sdpa_fallback`，注意它把 `enable_gqa` 也透传。

测试侧的回退判定镜像（复刻生产 `fallback()` 的 `any([...])`）：

[tests/test_ffpa_fwd.py:78-96](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_ffpa_fwd.py#L78-L96) —— `_is_sdpa_fallback_shape`，按 `forward_backend` 构造对应 Backend 实例，再列出与生产侧一致的五个回退条件。

真实生产侧回退分支——`ffpa_attn_func` 里那段带 `# HACK` 的代码，正是 `_sdpa_fallback` 要镜像的目标：

[src/ffpa_attn/ffpa_attn_interface.py:157-168](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ffpa_attn_interface.py#L157-L168) —— `fallback()` 命中时直接 `return torch._C._nn.scaled_dot_product_attention(...)`，注释 `# HACK: Avoid recursive for monkey-patch usage.`。

把这两段放一起读，就能看清「测试的 `_sdpa_fallback` 为什么必须用底层绑定」——它是在忠实复现生产代码的那一行。

#### 4.2.4 代码实践

**实践目标**：通过阅读一个真实用例，看清 `_is_sdpa_fallback_shape` 如何在 `causal + cross` 场景下决定参考实现。

**操作步骤**：

1. 打开 [tests/test_ffpa_fwd.py:1268-1304](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_ffpa_fwd.py#L1268-L1304)（`test_ffpa_attn_func_causal_cross_attention`）。
2. 跟踪它对 `ref` 的三选一分支：先 `_is_sdpa_fallback_shape`，再 `causal` 时手工构造 `attn_mask`（因为 SDPA 的 `is_causal` 只支持 `Nq==Nkv` 方阵情形），最后才 `_sdpa_ref`。

**需要观察的现象**：当 `Nq != Nkv` 且会回退时，它先尝试 `_sdpa_fallback(is_causal=True)`；若该原生调用本身抛 `RuntimeError`（某些形状 SDPA 不支持），测试转而断言 FFPA 也抛 `RuntimeError`——即「SDPA 不行的形状，FFPA 回退后也不行，且行为一致」。

**预期结果**：理解参考实现的选取是被测形状驱动的；这是「测试镜像生产逻辑」的典型范例。本步为纯阅读型实践，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `_sdpa_fallback` 用 `torch._C._nn...` 而 `_sdpa_ref` 用 `F.scaled_dot_product_attention`？两者算的结果有差别吗？

**答案**：数值结果相同（同一个算子），但**入口不同**。`_sdpa_ref` 是给「普通 SDPA 参考」用的；`_sdpa_fallback` 刻意走底层绑定，是为了忠实复现 FFPA 内部那条 `# HACK` 回退分支——那条分支必须用底层绑定以避开 monkey-patch 递归。测试要镜像的正是「入口」，而不只是「结果」。

**练习 2**：`_is_sdpa_fallback_shape` 为什么要按 `forward_backend` 分别构造 `CuTeDSLBackend` / `CUDABackend` / `TritonBackend`？

**答案**：因为不同后端的「是否允许小 D」由 `_backend_allows_small_d` 决定（受 `FFPA_TRITON_ALLOW_SMALL_D` / `FFPA_CUTE_ALLOW_SMALL_D` 等开关影响，见 u3-l3）。回退与否取决于后端实例，所以镜像判定必须用与被测调用相同的后端类型。

---

### 4.3 IS_ROCM 差异：dropout RNG 与数值容差

#### 4.3.1 概念说明

FFPA 跑在 AMD GPU 上时（Triton-AMD / HIP），有若干「和 NVIDIA 上不一致」的地方，测试必须显式处理，否则 CI 会假性失败。最典型的两类：

1. **dropout 掩码的 RNG 不一致**。FFPA 的 dropout 复刻了 SDPA 的 Philox 逐元素布局（见 u4-l4），在 NVIDIA 上能和原生 SDPA 逐位对齐；但 Triton-AMD 的 RNG 实现与原生 SDPA 不同，导致同一个 seed 产出不同掩码，dropout 测试无法逐元素比较。处理方式：**直接跳过**这些 dropout 用例（`pytest.skip` / `skipif`）。
2. **大归约的数值差异**。反向在大 seqlen × 大 head_dim × 高 GQA 比例下，Triton-AMD 的 FMA 收缩与 codegen 差异让梯度误差偏大。处理方式有两档：能容忍的**放宽容差**（`_tolerance` 在 ROCm 上整体放宽到 5e-2）；实在超标的**标记 `xfail`**（预期失败，不阻断 CI），并在注释里写清是哪张卡（如 gfx1101/RDNA3）的已知问题。

这一切都由一行探测开关驱动：

```python
IS_ROCM = hasattr(torch.version, 'hip') and torch.version.hip is not None
```

它在三个测试文件里都出现，是「跨厂商测试」的标准姿势。

#### 4.3.2 核心流程

```
                  IS_ROCM = (torch.version.hip is not None)
                              │
   ┌──────────────────────────┼──────────────────────────┐
   ▼                          ▼                          ▼
dropout 用例               容差函数                 已知精度缺陷
IS_ROCM → skip         IS_ROCM → 放宽到 5e-2      (N,D) 命中表 → xfail
（RNG 无法对齐）        （FMA/codegen 差异）       （gfx1101 dv/dk 偏差）
```

容差函数（前向）的基本表：

\[ \text{atol}=\text{rtol}=\begin{cases} 2\times10^{-2} & \text{bf16}\\ 1\times10^{-2} & \text{fp16 (NVIDIA)} \end{cases} \]

反向在 ROCm 上整体放到 \(5\times10^{-2}\)，bf16 也是 \(5\times10^{-2}\)；某些超大形状还会进一步分级放宽。

#### 4.3.3 源码精读

三个文件共享同一行 ROCm 探测（以 monkey-patch 文件为例）：

[tests/test_monkey_patch.py:25-26](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_monkey_patch.py#L25-L26) —— `IS_ROCM` 定义，注释点明「dropout mask RNG differs between Triton-AMD and native SDPA」。

前向 dropout 用例在 ROCm 上整段跳过：

[tests/test_ffpa_fwd.py:339-353](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_ffpa_fwd.py#L339-L353) —— `@pytest.mark.skipif(IS_ROCM, reason=...)` 挂在 `test_ffpa_attn_func_triton_dropout_matches_sdpa` 上。

反向容差函数：bf16 与 ROCm 都放到 5e-2，注释解释「FMA contraction differences and Triton-AMD codegen differences in large reductions」：

[tests/test_ffpa_bwd.py:38-45](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_ffpa_bwd.py#L38-L45) —— `_tolerance(dtype)` 的三分支。

ROCm 已知缺陷用 `xfail` 吸收（注意注释写清是 gfx1101/RDNA3 的 dv 精度问题，gfx90a 上不复现）：

[tests/test_ffpa_bwd.py:942-957](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_ffpa_bwd.py#L942-L957) —— `_CAUSAL_BWD_ROCM_XFAIL_BF16` 表 + 测试体里的 `pytest.xfail(...)`。

> 设计要点：`skip` 与 `xfail` 不是「掩盖问题」。`skip` 用在「该测试在此平台无意义」（RNG 本就无法对齐）；`xfail` 用在「这是已定位、已记录的厂商缺陷，等待上游修复」，两者都带明确 `reason`，方便日后排查。

#### 4.3.4 代码实践

**实践目标**：理解容差与平台耦合，能在不跑的情况下预测一个用例在 NVIDIA / ROCm 上的行为。

**操作步骤**：

1. 对照 [tests/test_ffpa_fwd.py:106-113](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_ffpa_fwd.py#L106-L113)（前向 `_tolerance`）与 [tests/test_ffpa_bwd.py:38-45](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_ffpa_bwd.py#L38-L45)（反向 `_tolerance`），列一张「dtype × 平台 × 方向 → atol/rtol」表。
2. 找出 [tests/test_ffpa_bwd.py:575-581](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_ffpa_bwd.py#L575-L581) 的 `skipif`，读它的 `reason`：gfx1101 的 SMEM 上限 64 KB 装不下 Triton-AMD autotuner 选中的 72 KB 配置，会崩 HIP runtime 并污染同进程后续测试。

**需要观察的现象**：容差不是「拍脑袋放宽」，而是按 dtype / 平台 / 形状规模分级的；越大的归约（长 seqlen、大 D、高 GQA）容差越宽（见 `test_ffpa_bwd_causal` 里 N≥16384 时放到 3e-1）。

**预期结果**：你能口头说出「bf16 反向、ROCm、N=16384」这三重叠加下的容差。本步为阅读型实践。

#### 4.3.5 小练习与答案

**练习 1**：为什么 dropout 测试在 ROCm 上是 `skip` 而不是「放宽容差」？

**答案**：因为 dropout 的误差不是「数值精度」问题，而是「掩码本身不同」——Triton-AMD 的 RNG 与原生 SDPA 不同，同一个 seed 产生不同的丢弃位置，输出差异是结构性的、不可用容差吸收。唯一诚实的做法是跳过。

**练习 2**：`xfail` 与 `skip` 对 CI 信号有什么不同？

**答案**：`skip` 表示「此用例在该平台不执行」，CI 既不算通过也不算失败；`xfail` 表示「此用例预期失败」，若真的失败了 CI 仍算通过（XPASS/XFAIL 机制），但如果哪天它**意外通过了**（xpass），CI 会提醒你——这正好用来监控「上游厂商缺陷是否已修复」。

---

### 4.4 monkey-patch 测试：_native_sdpa 与「回退不递归」锁定

#### 4.4.1 概念说明

`tests/test_monkey_patch.py` 锁定的是 FFPA 文档里那句公开用法：

```python
import torch.nn.functional as F
from ffpa_attn import ffpa_attn_func
F.scaled_dot_product_attention = ffpa_attn_func   # 一行接入
```

这个用法对 FFPA 提了**两个相反方向**的要求，测试必须同时盯住：

1. **大 D 必须真的走 FFPA**。如果 monkey-patch 后大 D 调用「意外回退」了，那 patch 就形同虚设——你以为换了 FFPA，其实还在跑 SDPA。
2. **小 D 回退必须走原生 SDPA，且不能递归**。小 D 时 `ffpa_attn_func` 内部会回退；如果回退分支调的是**已被 patch 过的** `F.scaled_dot_product_attention`，就会再次进入 `ffpa_attn_func` → 再次回退 → 再次进入……无限递归。生产代码用 `torch._C._nn...`（底层绑定）规避了这一点，测试要把这条规避措施「钉死」。

测试的杀手锏是一个**会抛异常的桩** `_block_native_sdpa`：它把底层绑定替换成一个「一旦被调用就 `raise AssertionError`」的函数。于是：

- 在大 D 用例里挂上这个桩 → 如果 FFPA 意外回退，就会撞上桩而报错 → 证明「大 D 没回退，走了 FFPA」。
- 在小 D 用例里**不挂**这个桩 → 让它正常回退到真实底层绑定 → 比较回退结果与原生 SDPA 是否一致 → 证明「回退路径正确且（因为没爆栈）没递归」。

而 `_native_sdpa` 则是测试自己的「调真实底层绑定」的小封装，用来在 patch **之前**算参考值。

#### 4.4.2 核心流程

```
小 D 用例（回退路径）：
  ref = _native_sdpa(q,k,v, scale=...)              # patch 前算参考
  monkeypatch F.sdpa = ffpa_attn_func               # 接管 Python 符号
  out = F.scaled_dot_product_attention(q,k,v,...)   # 内部回退→底层绑定（不递归）
  assert_close(out, ref)                            # 回退结果==原生 SDPA
  （没爆栈 ⇒ 证明没递归）

大 D 用例（FFPA 路径）：
  ref = _native_sdpa(q,k,v, **kwargs)               # patch 前算参考
  monkeypatch F.sdpa = ffpa_attn_func
  monkeypatch torch._C._nn.sdpa = _block_native_sdpa  # 把底层绑定换成「炸弹」
  out = F.scaled_dot_product_attention(q,k,v,...)   # 必须走 FFPA，否则撞炸弹
  assert_close(out, ref)                            # FFPA 输出==原生 SDPA
```

一句话：**小 D 测「回退对不对」，大 D 测「有没有回退」**；同一个「堵住原生入口」的技巧，在大 D 里是断言手段，在小 D 里则刻意不启用，从而放行真实回退。

#### 4.4.3 源码精读

测试自己的「真实底层绑定」封装：

[tests/test_monkey_patch.py:29-30](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_monkey_patch.py#L29-L30) —— `_native_sdpa`，转发到 `torch._C._nn.scaled_dot_product_attention`。

「炸弹」桩——一旦大 D 用例意外回退，就会撞上它：

[tests/test_monkey_patch.py:65-69](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_monkey_patch.py#L65-L69) —— `_block_native_sdpa`，`raise AssertionError("large-D monkey-patched case unexpectedly fell back to native SDPA")`。

小 D 用例：monkey-patch 后让回退自然走真实底层绑定，只比精度（不挂炸弹）：

[tests/test_monkey_patch.py:78-92](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_monkey_patch.py#L78-L92) —— `test_monkey_patched_sdpa_small_d_fallback_matches_native`，D=128，patch + 比对 `_native_sdpa` 参考。

大 D 用例：同时 patch Python 符号**和**底层绑定（后者换成炸弹），强制走 FFPA：

[tests/test_monkey_patch.py:101-136](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_monkey_patch.py#L101-L136) —— `test_monkey_patched_sdpa_large_d_ffpa_paths_match_native`，注意第 124-130 行的注释：解释挂炸弹是为了「防止用例变成两次原生 SDPA 调用的无意义比较」。

被锁定的生产代码——正是 [src/ffpa_attn/ffpa_attn_interface.py:157-168](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ffpa_attn_interface.py#L157-L168) 那条 `# HACK: Avoid recursive for monkey-patch usage.` 分支：回退时调底层绑定而非 Python 符号。把测试和生产这两段对照看，就能完整理解「为什么不递归」。

#### 4.4.4 代码实践

**实践目标**：用「读 + 推理」复现测试的两个核心断言，并预测若有人把生产代码的 `torch._C._nn...` 改回 `F.scaled_dot_product_attention` 会发生什么。

**操作步骤**：

1. 阅读 [tests/test_monkey_patch.py:101-136](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_monkey_patch.py#L101-L136)，确认大 D 用例同时 patch 了 `F.scaled_dot_product_attention`（换成 `ffpa_attn_func`）和 `torch._C._nn.scaled_dot_product_attention`（换成 `_block_native_sdpa`）。
2. 假设性修改（**不要真改源码**）：如果 [src/ffpa_attn/ffpa_attn_interface.py:159](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ffpa_attn_interface.py#L159) 的回退调用从 `torch._C._nn...` 改成 `F.scaled_dot_product_attention`，推演小 D 用例的执行轨迹。

**需要观察的现象**：

- 步骤 2 推演：小 D 时 `ffpa_attn_func` 回退 → 调 `F.scaled_dot_product_attention` → 但它已被 patch 成 `ffpa_attn_func` → 再次回退 → 再次调 `F...` → **无限递归直到爆栈（RecursionError）**。这正是生产代码那段 `# HACK` 要阻止的灾难，也是小 D 用例「不爆栈即通过」所隐式锁定的行为。

**预期结果**：你能清晰说出「大 D 用例靠炸弹证明走了 FFPA；小 D 用例靠不爆栈 + 精度匹配证明回退正确且不递归」。无需运行即可完成本推理型实践；若要在 GPU 上实际跑，命令为 `pytest tests/test_monkey_patch.py -v`（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：为什么大 D 用例要**同时** patch Python 符号和底层绑定？只 patch Python 符号行不行？

**答案**：不行。大 D 用例要证明「调用真的进了 FFPA kernel」。如果只 patch Python 符号，万一 `ffpa_attn_func` 内部对大 D 也意外回退，回退分支调的是底层绑定（未被 patch），就会静默返回原生 SDPA 结果——用例退化成「两次 SDPA 比较」，永远通过却没测到 FFPA。把底层绑定也换成炸弹后，任何回退都会立刻撞炸弹报错，从而强制断言「走了 FFPA」。

**练习 2**：`_native_sdpa` 和 `_block_native_sdpa` 都是对 `torch._C._nn.scaled_dot_product_attention` 的替换，它们的角色有何本质不同？

**答案**：`_native_sdpa` 是**透传**——原样调真实底层绑定，用来在 patch 前算「真值参考」；`_block_native_sdpa` 是**拦截**——任何调用都抛 `AssertionError`，用来在大 D 用例里充当「回退探测器」。一个提供 ground truth，一个提供负向断言信号。

**练习 3**：小 D 用例没有挂炸弹，它如何「证明没递归」？

**答案**：靠「能正常返回结果」。若发生递归，进程会在 `out = F.scaled_dot_product_attention(...)` 这行就 `RecursionError`，根本走不到 `assert_close`。所以小 D 用例「断言通过」本身就隐含了「回退调底层绑定、未进入被 patch 的 Python 符号、未递归」这一整条链条成立。

---

## 5. 综合实践

**任务**：为 FFPA 的前向正确性套件**新增一个测试形状并说明它落在哪条路径**，借此把本讲四个模块串起来。

要求：

1. 在 `CORRECTNESS_SHAPES`（[tests/test_ffpa_fwd.py:32-40](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_ffpa_fwd.py#L32-L40)）里**设想**追加一条 `(1, 4, 1024, 448)`（D=448，介于 320 和 512 之间）。
2. 用本讲学到的判定方法，回答：
   - 它会触发 `fallback()` 吗？用 `_is_sdpa_fallback_shape` 的五个条件逐条核对（提示：D=448 既不 ≤256、也不 >1024，Nq=1024 不在 `8<=Nq<512`，Nkv=1024 不 <512）。
   - 它走的是 FFPA kernel 还是回退？参考值该用 `_sdpa_ref` 还是 `_sdpa_fallback`？
   - 它会进 `test_ffpa_attn_func_matches_sdpa` 的精度比较吗？容差是多少（fp16 / bf16）？
3. 若你在 AMD GPU 上跑这条用例且**开启 dropout**，参考 [tests/test_monkey_patch.py:25-26](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_monkey_patch.py#L25-L26) 与 [tests/test_ffpa_fwd.py:339-342](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_ffpa_fwd.py#L339-L342)，预测它的命运。
4. 把以上结论写成一段「测试设计说明」，并在其中**引用本讲提到的至少三处永久链接**作为依据。

**验收标准**：你的说明应清楚区分「正确性层 vs 分发层」「kernel 路径 vs 回退路径」「NVIDIA 容差 vs ROCm 处理」，并正确预判 D=448 不会回退、应进 FFPA kernel 比较这一结论。（运行验证可选；若有 GPU，可临时把该形状加入后 `pytest tests/test_ffpa_fwd.py -k matches_sdpa -v`，待本地验证。）

> 注意：本任务只要求「设计说明」；按 worker 规则，你**不应**真的修改仓库源码或测试文件。

## 6. 本讲小结

- FFPA 前向测试把覆盖拆成两层：`CORRECTNESS_SHAPES` 管精度（少量形状 + `assert_close`），`DISPATCH_SHAPES` 管「能跑通」（HEADNUMS×HEADDIMS 笛卡尔积 + 只查 finite/shape）。
- 小 D（D≤256）形状在默认后端下走 `fallback()` 回退，所以正确性矩阵同时校验了「回退路径」与「FFPA kernel 路径」两条。
- 两个参考实现分工明确：`_sdpa_ref` 调 Python 层 SDPA，`_sdpa_fallback` 刻意调底层绑定 `torch._C._nn...` 以镜像 FFPA 内部那条 `# HACK` 回退分支；`_is_sdpa_fallback_shape` 是生产 `fallback()` 的测试侧镜像，决定该用哪个参考。
- `IS_ROCM` 探测驱动跨厂商处理：dropout 因 RNG 不可对齐而 `skip`；反向大归约因 FMA/codegen 差异而放宽容差或 `xfail`，并附 gfx1101/RDNA3 等明确 reason。
- `test_monkey_patch.py` 用一个「抛异常的桩」`_block_native_sdpa` 同时锁定两件事：大 D 用例挂炸弹证明「真走了 FFPA」；小 D 用例不挂炸弹、靠「不爆栈 + 精度匹配」证明「回退正确且不递归」。
- 被锁定的生产代码就是 [src/ffpa_attn/ffpa_attn_interface.py:157-168](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ffpa_attn_interface.py#L157-L168) 那条「回退调底层绑定」的 `# HACK` 分支——测试与生产互为佐证。

## 7. 下一步学习建议

- 下一篇 **u9-l2 持久化配置与自动调优模式测试** 会转向 `tests/test_persistent_autotune_config.py` 与 `tests/test_triton_autotune_mode.py`，看测试如何注入临时 config 目录、断言「就近匹配」与 fast/max 模式一致性——建议先读 u8-l2/u8-l3 建立背景。
- 想深入「fallback 判定本身」的读者，可重读 u3-l3（`FFPAAttnMeta.fallback` / `_should_use_aten_small_d_forward`），把它与本讲的 `_is_sdpa_fallback_shape` 镜像对照。
- 对 dropout RNG 复刻机制（Philox 逐元素布局）感兴趣的读者，可继续读 u4-l4 与 u5-1，理解为什么 NVIDIA 上能逐位对齐、ROCm 上不能。
- 若你打算给 FFPA 贡献新形状/新后端，本讲的「正确性层 + 分发层」双层策略是必须遵循的测试范式——参见 u9-l4 扩展指南。
