# pypto 算子测试：st 用例与精度对比

## 1. 本讲目标

u7-l3 结尾我们留了一句话：「反向 ST 测试以一份 torch golden 同时做 BF16-autograd 基准、FP64-autograd 金标准与 NPU kernel 的三方梯度对比，按 MARE/MERE/RMSE 比值加小值域计数判定精度」。本讲就把这台「精度裁判机器」完全拆开。学完后你应该能够：

1. 说出 pypto 算子 st（System Test，系统级精度测试）目录的组织方式：一个 `utils.py` 公共工具库 + 六个「前反向 × 三种量化粒度」的测试文件。
2. 复用 `create_input` 造数引擎与 `forward_test` / `backward_test_autograd` 通用接口，为一个新 pypto 算子写出完整的参数化精度用例。
3. 解释三方对比（kernel vs benchmark vs golden）的判定公式：MARE / MERE / RMSE 比值 + 小值域计数，以及 L0 / L1 / L2 精度等级的含义。
4. 对比 pypto 测试与 ascendc 算子 st 测试的框架差异，理解「比值判定」这种设计为什么对低精度（BF16）硬件测试是必要的。

## 2. 前置知识

- **三方对比（triplet comparison）**：被测对象不止和一份参考实现比，而是同时准备三份数据——(1) kernel 输出（NPU 上跑 pypto 算子）；(2) benchmark（bm，同一份 torch 参考实现以 BF16/FP32 在同精度下跑）；(3) golden（参考实现升到 FP64 跑，当作「无限精度真值」）。判定标准不是「kernel 离真值多近」，而是「kernel 的误差 / benchmark 的误差」这个**比值**——因为 BF16 本身就有舍入误差，一个正确实现的误差应该与 BF16 torch 实现同量级，只允许比它差一个受控倍数。
- **MARE / MERE / RMSE**：三个误差统计量，分别是最大相对误差、平均相对误差、均方根误差。相对误差分母加 `1e-7` 防除零。
- **小值域（small value domain）**：当真值 \( |g| \) 接近 0 时相对误差会爆炸（分母趋零），统计上失真。因此把元素按真值绝对值切成「大值域 / 小值域」两半：大值域算 MARE/MERE/RMSE，小值域只统计「绝对误差超阈值的元素个数」。
- **pytest 参数化**：`@pytest.mark.parametrize` 把一组参数组合展开成多个独立用例，每个用例有可读的 `id`。
- **STE 回顾**（u7-l2/u7-l3 已讲）：`(x.round() - x).detach() + x` 让 round 像恒等函数一样反传梯度，本讲 golden 里会反复见到它，不再展开推导。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py` | 测试公共库（467 行）：造数引擎、误差指标、三方判定、forward/backward 通用执行接口 |
| `tests/st/test_ai_infra_qat_symmetric_per_tensor.py` | per_tensor 前向 st（bit=8，Embedding 场景） |
| `tests/st/test_ai_infra_qat_symmetric_per_tensor_backward.py` | per_tensor 反向 st |
| `tests/st/test_ai_infra_qat_symmetric_per_channel.py` | per_channel 前向 st（bit=4，Lm Head 场景），本讲精读标本 |
| `tests/st/test_ai_infra_qat_symmetric_per_channel_backward.py` | per_channel 反向 st，本讲精读标本 |
| `tests/st/test_ai_infra_qat_asymmetric_per_group.py` | per_group 非对称前向 st（bit=2/3，group=128），本讲精读标本 |
| `tests/st/test_ai_infra_qat_asymmetric_per_group_backward.py` | per_group 非对称反向 st |
| `ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/tests/st/test_ai_infra_aggregate_hidden.py` | ascendc 算子 st 代表（第 4.5 节对比用） |

六个测试文件结构几乎逐行相同，是同一个模板的六次实例化——这本身就是本讲最重要的观察。

## 4. 核心概念与源码讲解

### 4.1 st 测试的组织方式与用例五段式模板

#### 4.1.1 概念说明

pypto 的 st 测试要回答一个问题：**「用 Python DSL 写出来的设备算子，算出来的数和 torch 在同精度下算的数，是不是一样准？」** 它不验证流程分支（那是 UT 的事，见 u8 单元），只验证**数值精度**。

组织方式是「1 + 6」：一个 `utils.py` 承载全部可复用逻辑；六个测试文件按「3 种量化粒度 × 前向/反向」矩阵排布，每个文件约 92~121 行，全部套用同一五段式模板。

#### 4.1.2 核心流程

一个测试文件从上到下的五段：

```text
① import 段        sys.path 补相对路径 → 导入被测 wrapper 与 utils 工具
② golden 工厂      create_xxx_golden(标量参数) 返回闭包 f(*tensors, is_golden)
③ run_single_test  按 (N, M, bit, ...) 造张量 → 调 forward_test / backward_test_autograd
④ parametrize      pytest 参数化列出所有 (shape, bit, eps) 组合
⑤ test_model       取设备号 → 遍历 DISTRIBUTION 逐分布执行 → 可选落 CSV
```

六个文件现有的参数化用例矩阵（依据各文件 `parametrize` 列表）：

| 测试文件 | 用例 (N, M, bit/group, eps) |
| --- | --- |
| symmetric_per_tensor（前/反向） | (153376, 2048, bit=8), (38344, 2048, bit=8) |
| symmetric_per_channel（前/反向） | (153376, 2048, bit=4), (38344, 2048, bit=4) |
| asymmetric_per_group（前/反向） | (1024, 2048, group=128, bit=2), (768, 2048, group=128, bit=3) |

N=153376/38344 是真实模型 Embedding / Lm Head 的行数，M=2048 是隐藏维——用例形状直接取自生产配置。

#### 4.1.3 源码精读

先看 import 段（以 per_channel 前向为例）：

- [pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/st/test_ai_infra_qat_symmetric_per_channel.py:15-17](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/st/test_ai_infra_qat_symmetric_per_channel.py#L15-L17) — `sys.path.append("./ops-nn/quant/ai_infra_pypto_qat")` 用的是**相对路径**，这意味着 pytest 必须从 `pypto/src` 目录启动，否则 `from op_code.ai_infra_pypto_qat import ...` 会失败；随后导入被测 wrapper 与 utils 的五个符号（`forward_test, create_input, collect_result, DISTRIBUTION`）。

golden 工厂（前向）：

- [test_ai_infra_qat_symmetric_per_channel.py:20-53](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/st/test_ai_infra_qat_symmetric_per_channel.py#L20-L53) — 闭包捕获 `eps/min_v/max_v` 三个标量。同一个函数体靠 `is_golden` 切换两种精度：benchmark 路径升 FP32，golden 路径升 FP64（[35-40 行](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/st/test_ai_infra_qat_symmetric_per_channel.py#L35-L40)），44 行是熟悉的 STE detach 技巧，48-51 行控制返回是否落回 BF16。**注意 golden 与 kernel 的公式必须逐行同构**——这正是 u7-l2 讲过的四步公式链，此处是它的 torch 版。

`run_single_test`：

- [test_ai_infra_qat_symmetric_per_channel.py:56-68](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/st/test_ai_infra_qat_symmetric_per_channel.py#L56-L68) — 由 `bit` 推出量化上下界 `min_v = -2^(bit-1)`、`max_v = 2^(bit-1) - 1`；weight 形状 (N, M)、scale 形状 (N, 1)（per_channel 每行一个 scale）；`golden_inputs` 只含张量，而 `pto_inputs` 还追加了 `eps/min_v/max_v` 三个标量——两组输入分开传，因为 golden 闭包已经捕获了标量，而 kernel wrapper 需要显式接收（呼应 u7-l1 的「kernel 签名即张量契约、无标注参数为 Host 标量」）。

`test_model` 驱动段：

- [test_ai_infra_qat_symmetric_per_channel.py:81-92](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/st/test_ai_infra_qat_symmetric_per_channel.py#L81-L92) — 设备号从环境变量 `TILE_FWK_DEVICE_ID` 读取（默认 0）；对 `DISTRIBUTION` 里每种分布各跑一次；`collect_result` 为 True 时把参数与指标追加写入 CSV。`collect_result` 在 utils.py 中初始化为 False（[utils.py:21](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py#L21)），是批量精度摸底的开关，日常判定测试时关闭。

一个值得注意的不一致：五个文件用 `TILE_FWK_DEVICE_ID`，唯独非对称反向用了 `ASCEND_DEVICE_ID`（[test_ai_infra_qat_asymmetric_per_group_backward.py:111](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/st/test_ai_infra_qat_asymmetric_per_group_backward.py#L111)）。两者默认值都是 0，单卡无感，多卡跑批时若只设其中一个变量，那个文件会静默跑到 0 号卡——读码时要留意这类历史痕迹。

#### 4.1.4 代码实践

1. **实践目标**：不运行任何代码，仅靠阅读建立六个文件的用例全景，并验证「同一模板六次实例化」的判断。
2. **操作步骤**：在 `pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/st/` 下用 `diff` 任意两个同粒度的前向/反向文件（例如 `diff test_ai_infra_qat_symmetric_per_channel.py test_ai_infra_qat_symmetric_per_channel_backward.py`），逐块比对差异；再统计每个文件 `parametrize` 列表里的用例元组。
3. **需要观察的现象**：前向/反向文件的金色工厂几乎相同，差异集中在：反向文件的输入张量多了 `.requires_grad_(True)`（[test_ai_infra_qat_symmetric_per_channel_backward.py:63-64](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/st/test_ai_infra_qat_symmetric_per_channel_backward.py#L63-L64)）、导入的 wrapper 换成 `_backward` 后缀、调用 `backward_test_autograd` 而非 `forward_test`（[68 行](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/st/test_ai_infra_qat_symmetric_per_channel_backward.py#L68)）。
4. **预期结果**：得到 4.1.2 节那张用例矩阵的亲手验证版；确认六个文件 diff 后的实质差异不超过「wrapper 名、输入梯度标记、执行接口、CSV 文件名」四处。
5. 以上为纯源码阅读型实践，无需 NPU。

#### 4.1.5 小练习与答案

**练习 1**：为什么 golden 工厂要把 `eps/min_v/max_v` 做成闭包捕获，而不是像 kernel 那样当参数传？
**答案**：`forward_test` 的通用接口约定 golden 函数签名是 `golden_func(*tensor_inputs, is_golden)`（见 [utils.py:335-356](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py#L335-L356) 的 docstring），框架只负责透传张量；标量超参由工厂在构造时烧进闭包，这样 bm 路径和 golden 路径 guaranteed 使用同一组标量，不会出现两条路径参数漂移。

**练习 2**：`test_model` 里为什么要 `for dis in DISTRIBUTION` 循环，而不是把 distribution 加进 `parametrize`？
**答案**：两种写法功能等价，差别在用例粒度与失败定位。当前写法一个 parametrize 用例内部遍历分布，任何分布失败都算该用例失败，CSV 里逐分布记一行；若放进 parametrize，pytest 会为每个 (shape × distribution) 生成独立用例 id，失败定位更细但用例数翻倍。当前 `DISTRIBUTION` 只启用了 `uniform_large` 一项（[utils.py:24-32](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py#L24-L32)，其余被注释），所以实际只跑一档。

### 4.2 造数引擎：`create_input` 与可控分布

#### 4.2.1 概念说明

精度测试的第一步是造出「有代表性的脏数据」。`utils.py` 的 `create_input` 把造数收敛为一个函数：给定形状、dtype、device、分布名和随机种子，产出可复现的张量。分布族覆盖了量化算子最怕的几种数据形态：常规均匀、极小值（考验小值域）、带均值漂移的正态、含千倍离群点的正态（考验 clip 路径）。

#### 4.2.2 核心流程

```text
create_input(shape, dtype, device, distribution, seed)
  ├─ torch.manual_seed(seed)          # 每次调用重置种子 → 同参数必得同数据
  ├─ 按 distribution 名分发:
  │    "uniform[low,high]"  → 正则解析出 low/high → uniform_
  │    "uniform_small"      → uniform(-0.001, 0.001)
  │    "uniform_large"      → uniform(-5.0, 5.0)
  │    "normal"             → μ~U[-100,100], σ~U[1,25] 的 randn
  │    "outlier"            → normal 基础上 0.1% 元素 ×1000
  ├─ .to(dtype)                       # 先 FP32 生成再转目标 dtype
  └─ .to(device)                      # 搬到 cpu / npu:0 / ...
```

#### 4.2.3 源码精读

- [pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py:39-63](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py#L39-L63) — 三个私有造数函数：`_uniform`、`_normal`（μ 与 σ 本身随机抽样，模拟真实权重的分布漂移）、`_outlier`（`torch.rand(shape) < 0.001` 做掩码，命中元素放大 1000 倍——量化 scale 会被离群点拉大，是检验 clip/round 链条的最佳毒药数据）。
- [utils.py:69-98](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py#L69-L98) — 公共入口签名与 `torch.manual_seed(seed)`（98 行）。种子默认 33，且**函数内**重置——这保证了跨机器、跨次运行的确定性，代价是同 seed 下先后两次 `create_input` 的随机序列会重叠（先造的 N×M 大张量和后造的 N×1 小张量共享序列前缀）。
- [utils.py:115-135](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py#L115-L135) — 分布名分发。`uniform[...]` 用正则 `r"uniform\[\s*([-+]?[0-9]*\.?[0-9]+)\s*,\s*([-+]?[0-9]*\.?[0-9]+)\s*\]$"` 解析任意区间（117 行），不认识的分布名抛 `ValueError` 列出合法选项（134 行）。
- [utils.py:137-145](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py#L137-L145) — 先 `.to(dtype)` 再 `.to(device)`：始终以 FP32 生成再转换，避免在低精度上直接做随机采样的分布失真。

#### 4.2.4 代码实践

1. **实践目标**：验证造数引擎的确定性与分布正确性（纯 CPU，无需 NPU）。
2. **操作步骤**：写一个 10 行脚本（示例代码，非仓库文件）：

   ```python
   import sys, torch
   sys.path.append("./ops-nn/quant/ai_infra_pypto_qat")
   from tests.utils import create_input

   a = create_input((4, 8), "bfloat16", "cpu", "uniform_large", seed=33)
   b = create_input((4, 8), "bfloat16", "cpu", "uniform_large", seed=33)
   c = create_input((4, 8), "bfloat16", "cpu", "uniform[0.001,0.1]", seed=33)
   print(torch.equal(a, b), a.dtype, a.float().min().item(), a.float().max().item())
   print(c.float().min().item(), c.float().max().item())
   ```

   在 `pypto/src` 目录下以 `python 脚本.py` 运行。
3. **需要观察的现象**：第一行应打印 `True bfloat16` 且 min ≥ -5、max ≤ 5；第二行 min ≥ 0.001、max ≤ 0.1。
4. **预期结果**：同 seed 两次调用逐位相等（确定性成立）；`uniform[low,high]` 字符串区间生效。本实践仅需 CPU 版 torch 与 torch_npu 可 import（utils.py 顶部 import 了 torch_npu），无 NPU 也能跑；若环境中缺少 torch_npu，则此脚本**待本地验证**。
5. 注意 `create_input` 的 dtype 参数实际传的是 torch dtype 对象（测试文件里传 `torch.bfloat16`），docstring 里写的字符串名只是说明。

#### 4.2.5 小练习与答案

**练习 1**：`_outlier` 分布为什么对 QAT 算子测试特别有价值？
**答案**：离群点会把 per_channel/per_group 的 scale 拉大，使绝大多数正常元素归一化后落到量化格点中央、精度受损，同时离群点自身触发 clamp 边界——一次造数同时激活 clip 与 round 两条最容易出错的路径。当前 `DISTRIBUTION` 注释掉了 outlier（[utils.py:24-32](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py#L24-L32)），说明例行 CI 只跑常规档，扩展分布属于待启用的能力。

**练习 2**：如果想让 weight 和 scale 使用不同的随机序列，最简单的改法是什么？
**答案**：两次 `create_input` 传不同的 `seed`（例如 weight 用 33、scale 用 34）。因为种子在函数内重置，不同 seed 即完全独立的序列；不改 utils.py 一行。

### 4.3 三方精度判定：`precision_compare_triple`、L0/L1/L2 与小值域

#### 4.3.1 概念说明

这是整个测试体系的裁判核心。设计动机：BF16 只有 8 位尾数，任何 BF16 实现相对 FP64 真值都有约 \(2^{-9}\) 量级的固有误差，如果直接要求「kernel 误差 ≤ 某绝对阈值」，阈值将很难标定——太松漏掉 bug，太紧误杀正确实现。于是改为**相对基准判定**：让 torch 的 BF16 实现当「及格线」，kernel 的误差只允许是及格线的受控倍数。

#### 4.3.2 核心流程

对每个输出张量，以 golden（FP64）为真值 \( g \)，把元素按 \( |g_i| \ge \tau \)（dtype 相关小值阈值）分成大值域与小值域两半：

**大值域**（对 kernel 输出 \( x \) 与 benchmark 输出 \( b \) 各算一遍）：

\[
\mathrm{MARE}(x, g) = \max_i \frac{|x_i - g_i|}{|g_i| + 10^{-7}}, \qquad
\mathrm{MERE}(x, g) = \frac{1}{n}\sum_i \frac{|x_i - g_i|}{|g_i| + 10^{-7}}
\]

\[
\mathrm{RMSE}(x, g) = \sqrt{\frac{1}{n}\sum_i (x_i - g_i)^2}
\]

**比值**（分母带小值域阈值兜底，防 bm 误差为零时除零）：

\[
r = \frac{\mathrm{metric}_{\text{kernel}}}{\max\left(\mathrm{metric}_{\text{bm}},\ \tau\right)}
\]

**小值域**：只数「绝对误差超过 \( e \)（dtype 相关误差阈值）的元素个数」：

\[
c(x) = \sum_{i:\,|g_i| < \tau} \mathbb{1}\left[\,|x_i - g_i| > e\,\right], \qquad
r_{\text{small}} = \frac{c(x_{\text{kernel}})}{\max\left(c(x_{\text{bm}}),\ 1\right)}
\]

**判定**（默认 L2 档 `thres=(2, 1.2, 1.2)`）：

\[
\text{PASS} \iff r_{\text{MARE}} \le 2 \ \wedge\ r_{\text{MERE}} \le 1.2 \ \wedge\ r_{\text{RMSE}} \le 1.2 \ \wedge\ r_{\text{small}} \le 2
\]

即：kernel 的最大相对误差最多是 torch 基准的 2 倍，平均相对误差与 RMSE 最多 1.2 倍，小值域坏点数最多 2 倍。

#### 4.3.3 源码精读

- [utils.py:149-163](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py#L149-L163) — 两张 dtype 阈值表：`small_value_thres_dict`（bf16: \(2^{-8}\)≈0.0039）与 `small_value_error_thres_dict`（bf16: \(2^{-16}\)≈1.5e-5）。含义：真值绝对值小于 \(2^{-8}\) 的元素划入小值域，其绝对误差超过 \(2^{-16}\) 才计为一个坏点。\(2^{-8}\) 正是 BF16 的 ULP 量级（8 位尾数），\(2^{-16}\) 约为两个 BF16 半 ULP——阈值表编码的是「低精度格式自身的舍入噪声地板」。
- [utils.py:166-170](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py#L166-L170) — `get_split_index`：以 golden 的绝对值与阈值比较生成 large/small 两个布尔掩码。**切分依据永远是 golden**，保证 kernel 与 bm 用同一套掩码，对比才公平。
- [utils.py:173-179](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py#L173-L179) — `compute_matrix_small_value`：小值域坏点计数；小值域为空时直接返回 0。
- [utils.py:182-196](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py#L182-L196) — `compute_matrix_large_value`：大值域三指标，相对误差分母 `+1e-7`（190 行）。
- [utils.py:199-213](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py#L199-L213) — `compute_re_matrix` / `compute_re_triplet_matrix`：比值计算。注意两个防御：bm 指标为 inf/nan 时比值取 1（放过），kernel 指标为 inf/nan 时取 1000（必杀）；分母 `max(bm_value, small_value_thres)` 兜底除零。
- [utils.py:216-220](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py#L216-L220) — 注释里的昇腾算子精度标准 2.1：L0（mare≤10, mere≤2, rmse≤2，常规算子）、L1（5/1.5/1.5，重要算子）、L2（2/1.2/1.2，关键算子）；函数默认形参 `thres=(2, 1.2, 1.2)` 即固定按 L2 最严档执行。
- [utils.py:220-263](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py#L220-L263) — `precision_compare_triple` 主体：三份数据先统一升 FP32 并搬回 CPU（uint8 走 `npu_dtype_cast` 特例，int8/int32 直接 `NotImplementedError`）；241 行切分掩码，244-246 行小值域比值（分母 `max(bm_error_count, 1)`），249-252 行大值域三元比值，254-261 行四条件 AND 出 PASS/FAILED。
- [utils.py:266-276](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py#L266-L276) — `compare`：打印五项指标后，`result != "PASS"` 即 `raise Exception("fail precision check")`。**这才是测试的硬门槛**——测试失败的唯一表现形式就是这个异常。

#### 4.3.4 代码实践

1. **实践目标**：用手算数字校准对指标体系的直觉。
2. **操作步骤**：写一个独立脚本（示例代码），手工构造 kernel/bm/golden 三个小张量，分别调用 `get_split_index`、`compute_matrix_large_value`、`compute_matrix_small_value`，与 numpy 手算对照：

   ```python
   import sys, torch, numpy as np
   sys.path.append("./ops-nn/quant/ai_infra_pypto_qat")
   from tests.utils import (get_split_index, compute_matrix_large_value,
                            compute_matrix_small_value, precision_compare_triple)

   g = torch.tensor([1.0, 1.0, 0.001, 0.0001])          # golden (fp32)
   x = torch.tensor([1.02, 0.99, 0.0012, 0.0001])       # 模拟 kernel
   b = torch.tensor([1.01, 0.995, 0.001, 0.0001])       # 模拟 bm
   large, small, tau = get_split_index(g, torch.float32)
   print(compute_matrix_large_value(x, g, large))
   print(compute_matrix_small_value(x, g, torch.float32, small))
   print(precision_compare_triple(x, b, g))
   ```

3. **需要观察的现象**：fp32 的小值域阈值是 \(2^{-14}\)，因此 0.001 与 0.0001 都落在大值域内（想触发小值域需把 g 的后两元素改成 1e-6 量级再试）；`precision_compare_triple` 返回 `(PASS, mare比值, mere比值, rmse比值, small比值)`。
4. **预期结果**：`compute_matrix_large_value` 返回的 MARE = max(0.02/1.0, 0.01/1.0) ≈ 0.02（分母 +1e-7 可忽略）；kernel 误差约为 bm 的 2 倍以内时 triple 判 PASS。运行需要 torch/torch_npu 环境，具体数值**待本地验证**。
5. 换用 bf16 dtype 表（threshold \(2^{-8}\)、error \(2^{-16}\)）重复一遍，观察小值域划分范围的变化。

#### 4.3.5 小练习与答案

**练习 1**：为什么小值域不算相对误差、只数坏点个数？
**答案**：真值 \( |g_i| \to 0 \) 时相对误差 \( |x_i-g_i|/(|g_i|+10^{-7}) \) 的分母趋零，单个本可忽略的绝对误差（如 1e-6）会被放大成 10 倍的相对误差，使 MARE 完全被小值元素绑架。改成「绝对误差 > \(2^{-16}\) 才计数」后，小值域衡量的是「超出格式噪声地板的坏点有多少个」，与大值域的相对指标互补。

**练习 2**：若 kernel 输出出现 NaN，`compute_re_matrix` 会给出什么结果？测试会怎样结束？
**答案**：[utils.py:200-203](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py#L200-L203) 中 kernel 侧 inf/nan 返回 1000，远超阈值 2/1.2，判定 FAILED；随后 `compare` 抛出 `Exception("fail precision check")`，pytest 把该用例记为失败。反向情况（bm 是 nan）比值取 1，不会因基准自身劣化而误杀。

### 4.4 通用执行接口：`forward_test` 与 `backward_test_autograd`

#### 4.4.1 概念说明

造数（4.2）与判分（4.3）之间还差一个「组织者」：安排三路执行、对齐输入输出、逐输出调裁判。这就是 `forward_test` 与 `backward_test_autograd`。前者面向前向算子（比输出），后者面向反向 kernel（比梯度），且**反向的基准梯度用 torch autograd 自动求导获得**——测试作者只写前向 golden，不必手写梯度公式。

#### 4.4.2 核心流程

`forward_test(inputs, pto_inputs, golden_func, pto_func)`：

```text
inputs ──clone──> bm_inputs（保持 requires_grad 标记）
       ──double─> golden_inputs（CPU FP64）
bm_out     = golden_func(*bm_inputs, is_golden=False)   # benchmark：FP32 计算
golden_out = golden_func(*golden_inputs, is_golden=True) # 金标准：FP64 计算
kernel_out = pto_func(*pto_inputs)                       # 被测 kernel
对每个输出 i：compare(kernel_out[i], bm_out[i], golden_out[i])   # 三方裁判
```

`backward_test_autograd(inputs, pto_inputs, golden_func, pto_func)`：

```text
① bm 前向（is_golden=False）→ 输出转 BF16 → randn 生成 grad_outputs
② bm 反向：torch.autograd.backward(bm_out, grad_outputs) → collect_grads 得 bm 梯度
③ golden 前向（FP64 CPU）→ grad_outputs 同步升 FP64 → autograd 反向 → golden 梯度
④ kernel 反向：pto_func(grad_outputs, *pto_inputs)  # 反向 kernel 直接吃 grad_outputs
⑤ 对每个 requires_grad 的输入 i：compare(pto_grad, bm_grad, golden_grad)
```

#### 4.4.3 源码精读

`forward_test`：

- [utils.py:335-367](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py#L335-L367) — 接口契约写在 docstring：golden_func 最后一个参数必须是 `is_golden`。三路输入由同一份 `inputs` 派生：`clone_inputs` 保真克隆（保留 requires_grad），`to_double_inputs` 升 FP64；365-367 行依次执行 bm / golden / kernel 三路前向，`normalize_outputs`（[314-317 行](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py#L314-L317)）把单输出包装成单元组，统一按元组遍历。
- [utils.py:371-384](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py#L371-L384) — 逐输出：**375 行的 `assert_allclose(kernel, bm, rtol=1e-3, atol=1e-3)` 包在 try/except 里只 `logger.error`，不使测试失败**——它是「kernel 与 torch 是否逐元素几乎一致」的观察性断言；真正的门槛是 378 行 `compare` 触发的比值判定。读码时极易把前者误当判定条件。

`backward_test_autograd`：

- [utils.py:399-413](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py#L399-L413) — bm 前向输出统一转 BF16（403 行）再以 `torch.randn` 造 `grad_outputs`（405 行）——用随机上游梯度而非全 1，是为了让每路梯度都被非平凡加权，暴露缩放类错误；随后 `torch.autograd.backward` 求出基准梯度，`collect_grads`（[320-332 行](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py#L320-L332)）按输入顺序收集，grad 为 None 直接抛异常。
- [utils.py:419-441](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py#L419-L441) — golden 路径：输入升 CPU FP64，`grad_outputs` 也同步 `detach().cpu().double()`（424 行），保证两条反向路径的**上游梯度逐位一致**（都源自同一个 BF16 randn），差异只来自实现本身。438-441 行调用 kernel 反向：单输出时 `pto_func(grad_outputs[0], *pto_inputs)`，多输出时展开——**第一个参数是 grad_outputs**，与 u7-l3 精读的反向 wrapper 签名 `ai_infra_qat_..._backward(grad_output, weight, scale, ...)`（[op_code/ai_infra_pypto_qat.py:379](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L379)）严格对应。
- [utils.py:450-467](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py#L450-L467) — 比较循环：只遍历「是 Tensor 且 requires_grad」的输入（452-453 行），kernel 梯度按出现顺序用 `idx` 对齐——这要求 `pto_func` 返回的梯度顺序与 inputs 中可导张量的顺序一致（例如 per_channel_backward 返回 `(grad_weight_out, grad_scale_out)`）。461 行同样是「只记日志」的 `assert_allclose`。

三个输入派生工具（[utils.py:279-311](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py#L279-L311)）：`clone_inputs` 用 `detach().clone()` 再恢复 requires_grad（避免克隆共享计算图），`to_double_inputs` / `_to_double_cpu_backward` 是 FP64 化的两个变体。

#### 4.4.4 代码实践

1. **实践目标**：跟踪 per_channel 反向测试一帧的完整数据流，弄清「梯度从哪来、到哪去」。
2. **操作步骤**：对照 [test_ai_infra_qat_symmetric_per_channel_backward.py:56-68](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/st/test_ai_infra_qat_symmetric_per_channel_backward.py#L56-L68) 与 [op_code/ai_infra_pypto_qat.py:379-387](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L379-L387)，在纸上画出五个张量/梯度集合的流转图：`weight(N,M)、scale(N,1)` → 前向输出 → `grad_outputs`（randn）→ 三路反向 → `(grad_weight, grad_scale)`。
3. **需要观察的现象**：bm 与 golden 两路的 grad_outputs 是**同一个** BF16 随机张量（golden 只是把它升 FP64），而 kernel 路直接消费它；三路比较时 weight 梯度与 scale 梯度各触发一次 `compare`。
4. **预期结果**：能回答「为什么 kernel 反向不需要 autograd」——因为反向 kernel 本身就是梯度的显式实现（u7-l2 推导的 grad×mask 与双路径 grad_scale 公式），autograd 只用于生成两路基准。本实践为源码阅读型，无需运行。
5. 额外验证（可选）：数一数 `run_single_test` 中带 `.requires_grad_(True)` 的张量个数（per_channel 是 2 个，非对称 per_group 是 3 个，见 [test_ai_infra_qat_asymmetric_per_group_backward.py:86-88](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/st/test_ai_infra_qat_asymmetric_per_group_backward.py#L86-L88)），与 kernel 返回的梯度路数一致。

#### 4.4.5 小练习与答案

**练习 1**：`forward_test` 里 375 行的 `assert_allclose(..., rtol=1e-3, atol=1e-3)` 失败时测试会挂吗？
**答案**：不会。它被 try/except 包住，异常仅进日志（[utils.py:374-377](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py#L374-L377)）；能让用例失败的只有 `compare` 在比值判定 FAILED 时抛出的异常。rtol/atol=1e-3 是「kernel≈torch」的观察阈值，供人工排查参考。

**练习 2**：为什么 `backward_test_autograd` 要在 403 行把 bm 前向输出转成 BF16 再生成 grad_outputs？
**答案**：让「上游梯度」以 BF16 随机数的形式固定下来，三路反向消费同一份 BF16 梯度（golden 侧只是升 FP64），使比较变量唯一化为「各实现自身的求导质量」；同时上游梯度带 BF16 量化噪声也更贴近真实训练场景中反向收到的梯度。

**练习 3**：per_group 前向测试的 golden 里为什么多做了一层形状防御（[test_ai_infra_qat_asymmetric_per_group.py:39-55](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/st/test_ai_infra_qat_asymmetric_per_group.py#L39-L55)）？
**答案**：per_group 的 scale/offset 形状是 `(num_groups, 1)`，其中 `num_groups = N*M/group_size` 由两个维度联合决定，配错 group_size 或忘算行数是高频错误；golden 在 `view(num_groups, group_size)` 之前先断言 2 维、M 整除 group_size、scale/offset 形状精确匹配，把「用例自身参数错误」与「kernel 精度问题」区分开，避免误导排查方向。

### 4.5 框架对比：pypto st 与 ascendc st

#### 4.5.1 概念说明

本仓库有两套 st 体系：pypto（本讲）与 ascendc（aggregate_hidden 等，u8-l3 将精读）。两者**判定哲学完全同源**——FP64 金标准 + 大小值域切分 + MARE/MERE/RMSE 比值 + L0/L1/L2 等级，但工程形态差异很大，根源在于被测对象的交付方式不同：pypto 算子是一个 `.py` 文件、import 即部署（u7-l1）；ascendc 算子必须先编译 run 包、安装到 CANN vendors、再经 torch_ops_extension 包装成 wheel（u1-l4、u6-l1）。

#### 4.5.2 核心流程（对比表）

| 维度 | pypto st | ascendc st |
| --- | --- | --- |
| 被测入口 | `from op_code.ai_infra_pypto_qat import ...`（Python wrapper 直接调用） | `import omni_training_custom_ops` 后调 `torch.ops.custom.*` |
| 前置条件 | 仅需 torch + torch_npu + NPU | 需先装算子 run 包 + 扩展 wheel 并 source 环境 |
| 组织风格 | 函数式：模块级 `utils.py` + 五段式模板文件 | 类式：继承 `torch_npu.testing.testcase.TestCase` |
| 用例参数 | `pytest.mark.parametrize` 列表 | TestCase 方法 + 类内 shape 配置 |
| 精度等级 | `precision_compare_triple` 固定 L2 档 `(2, 1.2, 1.2)` 形参 | `precision_levels_config` 字典，默认 L2 可按用例切换 |
| 阈值表 | 两个平行 dict（threshold / error） | `small_value_config` 嵌套 dict（同数值） |
| 失败动作 | `compare` 抛 Exception | 判定函数返回 False → assert |
| 设备选择 | `TILE_FWK_DEVICE_ID`（一处 `ASCEND_DEVICE_ID`） | pytest.ini / conftest 约定（u8-l3、u8-l4 详述） |

#### 4.5.3 源码精读

- [ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/tests/st/test_ai_infra_aggregate_hidden.py:32-45](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/tests/st/test_ai_infra_aggregate_hidden.py#L32-L45) — ascendc 侧的 `precision_levels_config`（L0/L1/L2 三档字典）与 `small_value_config`（bf16: threshold \(2^{-8}\)、error \(2^{-16}\)，与 pypto 的 [utils.py:149-163](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py#L149-L163) 数值完全一致）。同一套「昇腾算子精度标准 2.1」在两套框架里各自落地。
- [test_ai_infra_aggregate_hidden.py:132-152](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/tests/st/test_ai_infra_aggregate_hidden.py#L132-L152) — `normal_domain_pass`：同样计算 `npu_metrics / (gpu_metrics + 1e-7)` 的比值再与等级上限比较——**比值判定范式两边一致**，差别只在 ascendc 侧基准是「gpu」（torch 参考实现）指标、等级可配置，pypto 侧基准是 bm 指标带小值域阈值兜底、等级写死 L2。
- 回看 pypto 侧的组织：`utils.py` 以纯函数 + 模块级常量提供服务，六个测试文件只有 92~121 行；ascendc 侧每个算子把指标函数、配置、用例类全部写进一个 300 行的测试文件。前者复用强、后者自包含强（ascendc 算子目录相互独立发布，不便共享一个 utils）。

#### 4.5.4 代码实践

1. **实践目标**：亲手验证「同一精度标准、两种工程落地」的判断。
2. **操作步骤**：并排打开 [utils.py:216-220](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py#L216-L220)（注释版 L0/L1/L2 标准）与 [test_ai_infra_aggregate_hidden.py:32-45](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/tests/st/test_ai_infra_aggregate_hidden.py#L32-L45)（dict 版），逐项核对 L0/L1/L2 数值与 bf16/fp16/fp32 的小值域阈值；再对比两边的比值计算函数（`compute_re_matrix` vs `normal_domain_pass` 内的 ratio 推导）。
3. **需要观察的现象**：数值完全一致；结构差异是 pypto 用 `max(bm, small_value_thres)` 兜底而 ascendc 用 `+1e-7` 兜底。
4. **预期结果**：得出结论——两套框架是同一精度标准在不同交付形态（Python 算子 vs 编译算子包）下的镜像实现；为 ascendc 算子写 st 时可把 pypto 的 `utils.py` 当作可移植的判定参考实现。纯阅读型实践。
5. 若想把 pypto 的 utils 移植给 ascendc 用，注意 `torch_npu.npu_dtype_cast` 的 uint8 特例与 `precision_compare_triple` 对 int8/int32 的显式拒绝。

#### 4.5.5 小练习与答案

**练习 1**：为什么 pypto 测试不需要任何编译安装步骤，而 ascendc st 必须先装 run 包和 wheel？
**答案**：pypto 算子本身就是 Python 源码，`@pypto.frontend.jit` 在 import/首次调用时即时编译为设备代码（u7-l1 的「import 即部署」）；ascendc 算子是 C++/Ascend C 源码，必须经 build.sh 编译成 run 包安装到 CANN vendors、再经 torch_ops_extension 的 wheel 把 aclnn 符号包装成 `torch.ops.custom`（u6-l1），测试才能调到它。

**练习 2**：两套框架的小值域阈值表为何数值相同？
**答案**：两者都实现「昇腾算子精度标准 2.1」的同一套 dtype 噪声地板定义——阈值取各格式 1 个 ULP 量级（bf16 \(2^{-8}\)、fp16 \(2^{-11}\)、fp32 \(2^{-14}\)），坏点判据取更严的绝对误差（\(2^{-16}\) 等）。标准与被测算子的实现语言无关，故两套框架照抄同一张表。

## 5. 综合实践

**任务**：为 `test_ai_infra_qat_symmetric_per_channel.py` 补一个新用例——N=512、M=768、scale 逐通道随机（正值区间），断言与 CPU 参考实现的误差在阈值内。

**第一步：约束核对。** docs 对 per_channel 的约束是「weight 为 2 维 (N, M)，M∈[128, 3072] 且被 128 整除，BF16」（[docs/qat_ops.md:354-356](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/docs/qat_ops.md#L354-L356)）。M=768 = 6×128，落在 [128, 3072] 内且整除 128，合法。

**第二步：写用例。** 两条路任选或都做。

路线 A——最小改动，直接加一行参数化（与现有用例同分布造数）：

```python
# 在 test_ai_infra_qat_symmetric_per_channel.py 的 parametrize 列表追加
(512, 768, 4, 0.0001),
```

路线 B——新增独立用例函数，scale 用正值区间逐通道随机（示例代码，待加入测试文件）：

```python
def run_scale_positive_test(N, M, bit, eps, device_id):
    device = f"npu:{device_id}"
    seed = 33
    min_v = float(-2**(bit-1))
    max_v = float(2**(bit-1) - 1)
    weight = create_input((N, M), torch.bfloat16, device, "uniform_large", seed)
    # scale 逐通道随机：正值区间 uniform[0.001, 0.1]，每行一个独立 scale
    scale = create_input((N, 1), torch.bfloat16, device, "uniform[0.001,0.1]", seed + 1)
    golden_inputs = [weight, scale]
    pto_inputs = [weight, scale, eps, min_v, max_v]
    golden = create_symmetric_qat_nscale_golden(eps, min_v, max_v)
    return forward_test(golden_inputs, pto_inputs, golden,
                        ai_infra_qat_symmetric_per_channel)


def test_scale_per_channel_positive():
    device_id = int(os.environ.get('TILE_FWK_DEVICE_ID', 0))
    torch.npu.set_device(device_id)
    run_scale_positive_test(512, 768, 4, 0.0001, device_id)
```

要点说明：

- scale 换 `seed + 1` 避免与 weight 共享随机序列前缀（见 4.2.3 对 `manual_seed` 重置的分析）；正值区间让每行 scale 落在真实量化的合理量程，同时仍有个别行可能贴近 eps（0.001 > eps=1e-4，如需压测 eps 保护可再补一档 `uniform[0, 0.0002]`）。
- 「断言误差在阈值内」由 `forward_test` 内部完成：`assert_allclose(rtol=1e-3, atol=1e-3)` 只记日志，真正的判分是 `compare` → `precision_compare_triple` 的 L2 档比值 `(2, 1.2, 1.2)` + 小值域比值 ≤ 2，FAILED 会抛异常使 pytest 失败。

**第三步：运行（需要 NPU + torch/torch_npu 环境）。** 由 `sys.path.append("./ops-nn/quant/ai_infra_pypto_qat")`（相对路径）推知须在 `pypto/src` 目录下启动：

```bash
cd pypto/src
TILE_FWK_DEVICE_ID=0 pytest tests/st/test_ai_infra_qat_symmetric_per_channel.py -v -k "512"
```

**阈值取值依据与待确认项**：

- 比值阈值 `(2, 1.2, 1.2)` 与小值域阈值 ≤ 2 直接沿用 `precision_compare_triple` 默认形参（[utils.py:220](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py#L220)），依据是注释标注的「昇腾算子精度标准 2.1」L2（关键算子）档——QAT 伪量化输出直接参与权重更新，按关键算子取最严档合理。
- `rtol/atol=1e-3` 的观察性断言阈值取自 `forward_test` 现有实现（[utils.py:375](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py#L375)）；**待确认**：该 1e-3 是否适配 N=512 的小形状（现有用例均为 3 万行以上大张量，小张量下 MARE 的最大值统计更敏感，比值可能波动），需在真实 NPU 环境跑一遍确认通过情况——本讲写作环境无 NPU，**待本地验证**。
- 无 NPU 环境时的替代验证：只做路线 A/B 的代码编写并用 `python -m py_compile` 校验语法；同时用 4.3.4 节的 CPU 脚本单独验证 golden 闭包本身正确（把 `ai_infra_qat_symmetric_per_channel` 换成 golden 的 FP32 路径自比对）。

## 6. 本讲小结

- pypto st 是「1 个 utils.py + 6 个模板文件」：`create_input` 造数（五种分布、种子可控）→ `forward_test` / `backward_test_autograd` 组织三路执行 → `precision_compare_triple` 判分。
- 判定采用**三方比值**范式：kernel 与 bm（torch 同精度实现）各自对 FP64 golden 算 MARE/MERE/RMSE，要求 kernel 误差 ≤ bm 误差的 L2 档倍数（2/1.2/1.2），小值域只数「绝对误差超 \(2^{-16}\)（bf16）」的坏点且比值 ≤ 2。
- 反向测试不需要手写梯度：bm 与 golden 两路的梯度由 torch autograd 自动求出，kernel 反向以 `grad_outputs` 为首参直接调用；三路消费同一份 BF16 随机上游梯度。
- `assert_allclose(rtol=1e-3, atol=1e-3)` 只记日志不判生死，唯一的硬门槛是 `compare` 抛出的异常——读这套测试时务必分清两者。
- 与 ascendc st 对比：判定标准同源（同一张 L0/L1/L2 与小值域阈值表、同样的比值判定），工程形态迥异（函数式 vs TestCase 类、免编译 vs 需装 run 包 + wheel、固定 L2 vs 可配置等级）。
- 细节警觉点：`sys.path.append` 相对路径决定必须从 `pypto/src` 启动 pytest；非对称反向用 `ASCEND_DEVICE_ID` 而其余五个文件用 `TILE_FWK_DEVICE_ID`。

## 7. 下一步学习建议

本讲完成了 u7 单元（pypto 算子开发）的最后一讲。下一站进入 u8 单元「测试体系：UT 与 ST」：

- **u8-l1 / u8-l2**：看 ascendc 侧的 UT（单元测试）如何用 faker 在无硬件环境验证 tiling 流程与分支——与本讲的「数值精度 ST」互补，呼应 u3-l4 的 stub 桩机制。
- **u8-l3**：精读 `test_ai_infra_aggregate_hidden.py` 的完整判定链（本讲 4.5 节只对比了配置与比值函数），重点看 TestCase 类组织、小值域 pass 函数与 L0/L1/L2 等级切换。
- **u8-l4**：串起测试运行链路——`build.sh -u` 的 UT 构建分支与 pytest 在 st 目录的执行方式；可顺手把本讲综合实践的用例在真实环境跑通。

若想继续深挖 pypto 本身，建议回到 [op_code/ai_infra_pypto_qat.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py) 对照本讲的 golden 逐行核对六条公式链，并尝试把 `DISTRIBUTION` 里注释掉的 `outlier` 档启用，观察小值域坏点计数的变化。
