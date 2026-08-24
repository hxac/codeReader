# pypto 算子测试：st 用例与精度对比

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 pypto 算子 st 测试的目录组织：`tests/utils.py` 公共工具箱 + `tests/st/` 下六个用例文件（前向/反向 × 三种量化粒度），并知道如何用 pytest 运行它们。
2. 复用 `utils.py` 的工具组织参数化用例：`create_input` 造数、`forward_test` / `backward_test_autograd` 驱动、`DISTRIBUTION` 控制分布。
3. 讲清楚精度判定的完整逻辑：大值域 MARE/MERE/RMSE 三比值 + 小值域错误计数比，以及 L0/L1/L2 三级精度阈值。
4. 为一个量化算子独立写出新用例，并为 atol/rtol（或比值阈值）给出取值依据。
5. 对比 pypto 测试框架与 ascendc 的 st 测试框架（第 8 单元 u8-l3 的前哨）在组织方式与对比对象上的差异。

## 2. 前置知识

本讲是第 7 单元收官，默认你已读完前三讲：

- **u7-l1 pypto 编程模型**：kernel 签名即张量契约，wrapper 用 `torch.empty` 分配输出后按位置传入（目标传递风格）。本讲的「被测对象」就是这些 wrapper，如 `ai_infra_qat_symmetric_per_channel(weight, scale, eps, min_v, max_v)`。
- **u7-l2 / u7-l3 QAT 算法**：前向公式链（scale 防零保护 → 归一化 → round+clamp 伪量化 → 反量化）与 STE 直通估计器（`x + (round(x) - x).detach()`）。本讲的 golden 函数就是这些公式的 torch 复写。

还需要几个测试领域的基础概念：

- **golden（金标准）**：用高精度、结构简单的参考实现算出的「标准答案」。本仓库的 golden 用 CPU 上的 FP64 torch 实现——公式与 kernel 相同，但精度和实现路径不同，因此可以作为独立参照。
- **benchmark（基准，bm）**：比 golden 精度低一档、但「诚实」的对照实现。本仓库用 NPU 上的 FP32 实现，它代表「一个正常的工程实现在该数据类型下理应达到的精度」。
- **MARE / MERE / RMSE**：三个误差统计量，分别是最大相对误差、平均相对误差、均方根误差。昇腾算子精度标准（utils.py 注释中称「昇腾算子精度标准 2.1」）用它们分级。
- **小值域**：绝对值极小的元素。相对误差在分母接近 0 时会爆炸，所以这些元素要单独处理——只统计「误差超过绝对阈值」的个数，而不算相对误差。
- **pytest 参数化**：`@pytest.mark.parametrize` 把一组参数元组展开成多个独立用例，每个用例有字符串 id，可用 `-k` 按 id 过滤。

一个容易困惑的点先说清楚：**本讲的测试不直接使用 `torch.testing.assert_close` 那种绝对 atol/rtol 作为最终判据**。`forward_test` 里确实有一处 `assert_allclose(rtol=1e-3, atol=1e-3)`，但它被 try/except 包住、失败只打日志（下文 4.4 详述）；真正的通过/失败判定是「kernel 误差相对 benchmark 误差的比值不超过阈值」。这是为了公平对待 BF16 这类天然低精度数据类型。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py) | 公共测试工具箱：造数、精度判定、前向/反向通用驱动，全仓库 pypto 测试唯一的基础设施 |
| [tests/st/test_ai_infra_qat_symmetric_per_channel.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/st/test_ai_infra_qat_symmetric_per_channel.py) | 对称逐通道量化前向 st 用例（本讲主标本之一） |
| [tests/st/test_ai_infra_qat_symmetric_per_channel_backward.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/st/test_ai_infra_qat_symmetric_per_channel_backward.py) | 对称逐通道量化反向 st 用例（autograd 驱动的样本） |
| [tests/st/test_ai_infra_qat_asymmetric_per_group.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/st/test_ai_infra_qat_asymmetric_per_group.py) | 非对称分组量化前向 st 用例（带完整 shape 校验的 golden 样本） |
| tests/st/ 下其余三个文件 | `test_ai_infra_qat_symmetric_per_tensor.py`、`test_ai_infra_qat_symmetric_per_tensor_backward.py`、`test_ai_infra_qat_asymmetric_per_group_backward.py`，与前三个同构，共同构成「前反向 × 三粒度」矩阵 |
| [op_code/ai_infra_pypto_qat.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py) | 被测对象：六个量化算子的 kernel + wrapper |
| [docs/qat_ops.md](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/docs/qat_ops.md) | 算子约束文档（新用例的 shape 合法性依据） |

## 4. 核心概念与源码讲解

### 4.1 st 测试组织全景：六个用例文件的矩阵与运行方式

#### 4.1.1 概念说明

pypto 的 st（system test，系统级精度测试）不依赖任何独立测试工程——没有 conftest.py、没有 pytest.ini，每个算子包自带一个 `tests/` 目录：

```text
ai_infra_pypto_qat/
├── op_code/ai_infra_pypto_qat.py   # 被测算子（kernel + wrapper）
├── docs/qat_ops.md                  # 算法规格与约束
└── tests/
    ├── utils.py                     # 公共工具箱（本讲主角）
    └── st/                          # 六个用例文件 = 前向/反向 × 三种量化粒度
        ├── test_ai_infra_qat_symmetric_per_tensor.py
        ├── test_ai_infra_qat_symmetric_per_tensor_backward.py
        ├── test_ai_infra_qat_symmetric_per_channel.py
        ├── test_ai_infra_qat_symmetric_per_channel_backward.py
        ├── test_ai_infra_qat_asymmetric_per_group.py
        └── test_ai_infra_qat_asymmetric_per_group_backward.py
```

「前反向 × 三粒度」全覆盖不是巧合：QAT 算子的正确性 = 前向伪量化正确 + STE 反向梯度正确，两者数学上独立，必须分别验证；而 per_tensor / per_channel / per_group 三种粒度的 scale 形状不同（(1,1) / (N,1) / (num_groups,1)），切分与归约路径各自不同，也要分别覆盖。

#### 4.1.2 核心流程

一个用例文件的标准结构（四个固定角色）：

1. **golden 工厂函数**（`create_xxx_golden`）：返回一个闭包，闭包签名 `(tensor...) -> output`，末位固定是 `is_golden` 布尔开关；标量参数（eps、min_v 等）由工厂通过闭包捕获。
2. **`run_single_test`**：造数（`create_input`）、组装 `inputs`（golden 侧张量）与 `pto_inputs`（kernel 侧张量 + 标量）、调用通用驱动。
3. **参数化表**：`@pytest.mark.parametrize` 列出 (N, M, bit, eps...) 元组，每条展开为一个用例，id 形如 `N153376-M2048-bit4-eps0.0001`。
4. **`test_model`**：读环境变量选设备，遍历 `DISTRIBUTION` 中启用的分布逐一跑 `run_single_test`，可选把结果写 CSV。

#### 4.1.3 源码精读

六个文件共享同一个入口约定——`sys.path.append("./ops-nn/quant/ai_infra_pypto_qat")` 是相对路径，意味着 **pytest 必须从 `pypto/src` 目录启动**，否则 `from op_code.ai_infra_pypto_qat import ...` 会直接 ModuleNotFoundError：

[tests/st/test_ai_infra_qat_symmetric_per_channel.py:L11-L17](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/st/test_ai_infra_qat_symmetric_per_channel.py#L11-L17)
```python
import os
import torch
import pytest
import sys
sys.path.append("./ops-nn/quant/ai_infra_pypto_qat")
from op_code.ai_infra_pypto_qat import ai_infra_qat_symmetric_per_channel
from tests.utils import forward_test, create_input, collect_result, DISTRIBUTION
```
这段做了两件事：把算子包根目录挂进 `sys.path`（决定运行目录）、导入被测 wrapper 与 utils 工具。

设备选择走环境变量，默认 0 号卡：

[tests/st/test_ai_infra_qat_symmetric_per_channel.py:L81-L86](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/st/test_ai_infra_qat_symmetric_per_channel.py#L81-L86)
```python
def test_model(N, M, bit, eps) -> None:
    device_id = int(os.environ.get('TILE_FWK_DEVICE_ID', 0))
    torch.npu.set_device(device_id)
    results = []
    for dis in DISTRIBUTION:
        compare_result = run_single_test(N, M, bit, eps, dis, device_id)
```

分布清单是一个可编辑的开关表——当前只启用了 `uniform_large`，其余四种被注释，需要加深验证时手工放开：

[tests/utils.py:L24-L32](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py#L24-L32)
```python
DISTRIBUTION = [
    "uniform_large",
    # "uniform_small",
    # "uniform[-10, 10]",
    # "normal",
    # "outlier",
]
```
对量化算子而言 `outlier`（0.1% 元素放大 1000 倍）尤其有价值：极端值会大量落入 clamp 区间，专门压力测试伪量化的截断路径与 STE 掩码。

参数化表给出的 shape 都是真实模型尺寸（N=153376 的 embedding 级大张量、N=38344 的奇数行——顺带覆盖尾块处理）：

[tests/st/test_ai_infra_qat_symmetric_per_channel.py:L71-L80](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/st/test_ai_infra_qat_symmetric_per_channel.py#L71-L80)

#### 4.1.4 代码实践

1. **实践目标**：确认六个用例文件的矩阵关系与运行前置条件。
2. **操作步骤**：
   - 在 `pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/st/` 下执行 `ls test_*.py`，画一张 3 粒度 × 前反向的矩阵表，标注每个文件测试的 wrapper 函数名。
   - 在 `pypto/src` 目录（注意：必须是这一级）执行收集命令：
     ```bash
     cd pypto/src
     pytest --collect-only ops-nn/quant/ai_infra_pypto_qat/tests/st/ -q
     ```
   - 再故意换到 `pypto/src/ops-nn/quant/ai_infra_pypto_qat` 目录执行同样的收集命令，观察报错。
3. **需要观察的现象**：collect-only 应列出每个文件 × 每个参数元组 × 每个分布的用例（如 per_channel 前向 = 2 组参数 × 1 个分布 = 2 条）；换目录后应出现 `ModuleNotFoundError: No module named 'op_code'` 类错误，印证 `sys.path.append` 的相对路径约束。
4. **预期结果**：理解「文件数 = 6、用例数 = 参数数 × 启用分布数」，以及运行目录约束。
5. 本环境无 NPU 且未装 torch_npu，`pytest --collect-only` 是否能在 import 阶段通过**待本地验证**（collect 阶段会执行模块级 import，进而 import torch_npu 与 pypto 运行时）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 pypto 不像 ascendc 那样建一个集中的 `src/tests/st` 大目录，而是每个算子包自带 `tests/`？

**答案**：pypto 算子的交付单位是「一个 .py 文件 + import 即部署」（u7-l1 的结论），算子包是自包含的；测试随包走，使得任何一个算子目录可以被单独拷走、单独验证。ascendc 则是 C++ 编译型工程，测试与算子包共用一套 CMake/pytest 基础设施，集中管理更划算。代价是 pypto 每个包各自维护一份 `utils.py`——事实上本仓库也只有这一个 pypto 算子包，复制问题尚未出现。

**练习 2**：六个文件为什么不能合并成三个（前向、反向各写一个循环遍历三种粒度）？

**答案**：三种粒度的 golden 公式、输入张量清单（per_group 还多一个 offset）、scale 形状、参数化表（per_group 多 group_size/clip_val）都不同，合并会让单个文件承载三套差异巨大的 setup；拆开则每个文件独立可跑、失败定位直接对应一个 wrapper 函数。这是「一文件一被测对象」的测试组织惯例。

### 4.2 数据生成器 create_input：分布族与可复现性

#### 4.2.1 概念说明

`create_input` 是所有用例唯一的造数入口。它解决三个问题：**形状与 dtype**（任意 torch dtype）、**数值分布**（五种分布族，决定测试难度）、**可复现性**（固定 seed，失败可重放）。分布的选择直接影响精度测试的覆盖面：均匀分布是「温和」场景，normal 带大均值偏移，outlier 制造极端值冲击量化截断。

#### 4.2.2 核心流程

```text
create_input(shape, dtype, device, distribution, seed=33)
  ├─ torch.manual_seed(seed)          # 每次调用重置随机种子 → 完全可复现
  ├─ 按 distribution 分派到 _uniform / _normal / _outlier
  │    ├─ "uniform[low,high]"  → 正则解析 low/high，_uniform
  │    ├─ "uniform_small"      → _uniform(-0.001, 0.001)
  │    ├─ "uniform_large"      → _uniform(-5.0, 5.0)   # 当前唯一启用的
  │    ├─ "normal"             → μ∈[-100,100], σ∈[1,25] 随机抽取后 randn
  │    └─ "outlier"            → normal 基础上 0.1% 元素 ×1000
  ├─ .to(dtype)                       # 先 FP32 生成再转目标 dtype
  └─ .to(device)                      # 移动到 cpu / npu:0 等
```

注意生成顺序是「FP32 生成 → 转 dtype → 移设备」：随机性永远在 FP32 上产生，避免低精度 dtype 直接采样的分布畸变。

#### 4.2.3 源码精读

公共入口与种子固定：

[tests/utils.py:L69-L75](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py#L69-L75)
```python
def create_input(
    shape: Union[Tuple[int, ...], list],
    dtype: str = "float32",
    device: str = "cpu",
    distribution: str = "uniform_large",
    seed: int = 33,
) -> torch.Tensor:
```
配合 [tests/utils.py:L98](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py#L98) 的 `torch.manual_seed(seed)`，同一组参数两次调用产出完全相同的张量——测试失败后可以在本地逐元素重放。

三种底层分布生成器：

[tests/utils.py:L44-L63](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py#L44-L63)
```python
def _normal(shape):
    mu = np.random.uniform(-100, 100)
    sigma = np.random.uniform(1, 25)
    return torch.randn(shape, dtype=torch.float32) * sigma + mu

def _outlier(shape):
    tensor = _normal(shape)
    mask = torch.rand(shape) < 0.001
    tensor[mask] *= 1000.0
    return tensor
```
`_normal` 的 μ、σ 是每次调用重新抽取的——注意这两个抽取用的是 numpy 全局随机态，**不受** `torch.manual_seed` 控制，所以 normal/outlier 两种分布跨进程不可精确复现（这是一个值得留意的小坑；uniform 系不受影响）。

`uniform[low,high]` 自由区间用正则解析：

[tests/utils.py:L115-L123](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py#L115-L123)
```python
match = re.match(r"uniform\[\s*([-+]?[0-9]*\.?[0-9]+)\s*,\s*([-+]?[0-9]*\.?[0-9]+)\s*\]$", distribution)
...
low, high = map(float, match.groups())
tensor_fp32 = _uniform(shape, low, high)
```
这样新用例不必改 utils 就能指定任意区间，例如 `"uniform[-0.001,0.001]"` 专测小值域路径。

#### 4.2.4 代码实践

1. **实践目标**：直观感受五种分布的数值特征，为后续选分布建立手感。
2. **操作步骤**（CPU 即可，但 `import tests.utils` 会连带 `import torch_npu`，需已安装 torch_npu；未安装时标注待本地验证）：
   ```python
   # 在 pypto/src 下启动 python
   import sys; sys.path.append("./ops-nn/quant/ai_infra_pypto_qat")
   import torch
   from tests.utils import create_input
   for dis in ["uniform_large", "uniform_small", "uniform[-10,10]", "normal", "outlier"]:
       t = create_input((4096,), torch.bfloat16, "cpu", dis)
       print(f"{dis:20s} min={t.float().min():+.4f} max={t.float().max():+.4f} "
             f"mean={t.float().mean():+.4f}")
   ```
3. **需要观察的现象**：uniform_large 落在 ±5；uniform_small 在 ±0.001（大部分元素将落入小值域，触发 4.3 的小值域通道）；normal 均值漂移明显；outlier 的 max 约为 normal 的 1000 倍。
4. **预期结果**：五组统计量与上述描述一致；两次运行同一命令，uniform 系结果完全相同（seed 固定），normal/outlier 可能不同（numpy 随机态）。
5. 分布统计的具体数值**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `uniform_small`（±0.001）对量化算子是高难度用例？

**答案**：BF16 的小值域阈值是 \(2^{-8}\approx 0.0039\)（见 4.3），±0.001 的输入几乎全部落入小值域通道；同时 weight/scale 都极小时，归一化 `weight/scale` 的相对误差被放大，round 之后重建误差占比更高。它检验的是 kernel 在「数值普遍很小」时是否仍与 FP64 golden 保持一致，而不是被相对误差的除零噪声淹没。

**练习 2**：`create_input` 每次都 `torch.manual_seed(33)`，同一用例里 weight 和 scale 用同一 seed 生成，会不会导致两者相关、测试失真？

**答案**：会有轻度相关——seed 重置后两个张量消费的是同一条随机流的开头，shape 不同则元素级数值不同，但统计分布相同。对量化测试而言，关键性质（scale 的逐通道多样性、weight 的均匀覆盖）仍然成立，所以可接受；若要严格独立，可给 scale 换一个 seed（如 `seed=34`）。这是工程上「可复现优先」的折中。

### 4.3 精度判定引擎：双值域划分与 MARE/MERE/RMSE 三比值

#### 4.3.1 概念说明

这是本讲的核心模块，也是 pypto 测试框架与普通「assert 误差 < atol」测试的本质区别。设计动机有两个：

1. **相对误差在数值小时会爆炸**。\( |x-g|/|g| \) 当 \( g\to 0 \) 时趋于无穷，即使 x 与 g 都「足够准」。所以按 golden 的绝对值把元素分成**大值域 / 小值域**两池，分别用不同尺子。
2. **BF16 本身有固有精度极限**（尾数仅 8 位）。如果拿 kernel 输出直接和 FP64 golden 比绝对误差，任何 BF16 实现都会「不及格」。解法是引入第三个参赛者 benchmark（FP32 诚实实现），判定标准变为：**kernel 的误差不得超过 benchmark 误差的指定倍数**。能打赢「一个认真写的 FP32 实现」的 N 倍之内，就算合格。

#### 4.3.2 核心流程

设 golden 为 \( g \)、待比为 \( x \)。先按阈值 \( \mathrm{thres} \)（依 dtype 查表）划分：

\[ \mathrm{large} = \{i: |g_i| \ge \mathrm{thres}\}, \qquad \mathrm{small} = \{i: |g_i| < \mathrm{thres}\} \]

**大值域**：逐元素相对误差 \( \mathrm{re}_i = \dfrac{|x_i - g_i|}{|g_i| + 10^{-7}} \)（分母加 \(10^{-7}\) 防零），统计三个量：

\[ \mathrm{MARE} = \max_i \mathrm{re}_i, \qquad \mathrm{MERE} = \frac{1}{|L|}\sum_{i\in L} \mathrm{re}_i, \qquad \mathrm{RMSE} = \sqrt{\frac{1}{|L|}\sum_{i\in L}(x_i-g_i)^2} \]

**小值域**：不算相对误差，只数「绝对误差超阈值」的元素个数 \( \mathrm{cnt} = \sum_{i\in S} [||x_i-g_i| > \mathrm{errThres}] \)。

**三比值**（kernel 记 pto，基准记 bm，以 MARE 为例）：

\[ \mathrm{ratio}_{mare} = \frac{\mathrm{MARE}_{pto}}{\max(\mathrm{MARE}_{bm},\ \mathrm{thres})} \]

分母取 max 是防御：bm 太好（误差近 0）时退化为除以 thres，避免比值爆炸；inf/nan 有专门惩罚（见源码）。

**最终判定**（默认 L2 级阈值）：

\[ \text{PASS} \iff \mathrm{smallMatrix} \le 2 \ \wedge\ \mathrm{ratio}_{mare} \le 2 \ \wedge\ \mathrm{ratio}_{mere} \le 1.2 \ \wedge\ \mathrm{ratio}_{rmse} \le 1.2 \]

其中 \( \mathrm{smallMatrix} = \mathrm{cnt}_{pto}/\max(\mathrm{cnt}_{bm},1) \)。

阈值字典按 dtype 给出小值域划分阈值与错误阈值：

| dtype | 划分阈值 thres | 错误阈值 errThres |
| --- | --- | --- |
| float16 | \(2^{-11}\) | \(2^{-16}\) |
| bfloat16 | \(2^{-8}\) | \(2^{-16}\) |
| float32 | \(2^{-14}\) | \(2^{-30}\) |
| uint8 / float8_e4m3fn | \(2^{-4}\) | \(2^{-6}\) |

#### 4.3.3 源码精读

两个阈值字典（QAT 算子走 bfloat16 一行）：

[tests/utils.py:L149-L163](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py#L149-L163)
```python
small_value_thres_dict = {
        torch.float16: 2**-11,
        torch.bfloat16: 2**-8,
        torch.float32: 2**-14,
        torch.uint8: 2**-4, torch.float8_e4m3fn: 2**-4
    }
small_value_error_thres_dict = {
        torch.float16: 2**-16,
        torch.bfloat16: 2**-16,
        ...
```
BF16 的含义：绝对值小于 \(2^{-8}\approx0.0039\) 的 golden 元素进小值域；这些位置上 kernel 与 golden 的偏差超过 \(2^{-16}\approx1.5\times10^{-5}\) 就计一次错。

大值域三指标的计算（分母 +1e-7 防零）：

[tests/utils.py:L182-L196](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py#L182-L196)
```python
abs_diff = torch.abs(input_large - golden_large)
relative_error = abs_diff / (torch.abs(golden_large) + 1e-7)
mare = torch.max(relative_error).item()
mere = torch.mean(relative_error).item()
rmse = torch.sqrt(torch.mean((input_large - golden_large) ** 2)).item()
```

比值的防御性实现——bm 为 inf/nan 时返回 1（视为「bm 也不可靠，只要求 pto 不比 thres 差」），pto 为 inf/nan 时返回 1000（直接判死）：

[tests/utils.py:L199-L204](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py#L199-L204)
```python
def compute_re_matrix(input_value, bm_value, small_value_thres):
    if math.isinf(bm_value) or math.isnan(bm_value):
        return 1
    if math.isinf(input_value) or math.isnan(input_value):
        return 1000
    return input_value / max(bm_value, small_value_thres)
```

主判定函数与精度分级注释——**注释本身就是阈值的取值依据**（昇腾算子精度标准 2.1 的 L0/L1/L2 三级）：

[tests/utils.py:L216-L220](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py#L216-L220)
```python
# 昇腾算子精度标准2.1
# 精度等级 L0 thres: mare <= 10, mere <= 2, rmse <= 2 常规算子
# 精度等级 L1 thres: mare <= 5, mere <= 1.5, rmse <= 1.5 重要算子
# 精度等级 L2 thres: mare <= 2, mere <= 1.2, rmse <= 1.2 关键算子
def precision_compare_triple(pto_data, bm_data, golden_data, thres=(2, 1.2, 1.2)):
```
默认参数取 L2（关键算子级）。QAT 量化算子按关键算子要求——它们直接改写权重更新的梯度通路，出错会静默损伤训练收敛，理应用最严的一档。仓库未写明「为什么是 L2」，这一层级选择属隐含约定，**待确认**。

判定主体（uint8 有 hifloat8 特殊转换，浮点统一升 FP32 再比）：

[tests/utils.py:L241-L263](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py#L241-L263)
```python
large_value_idx, small_value_idx, small_value_thres = get_split_index(golden_data, dtype)

npu_error_count = compute_matrix_small_value(pto_data, golden_data, dtype, small_value_idx)
bm_error_count = compute_matrix_small_value(bm_data, golden_data, dtype, small_value_idx)
small_value_matrix = npu_error_count / max(bm_error_count, 1)
...
if small_value_matrix <= 2 and is_mare_acceptable and is_mere_acceptable and is_rmse_acceptable:
    result = "PASS"
```

外层 `compare` 把 FAILED 变成硬失败——测试因此以异常终止：

[tests/utils.py:L266-L276](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py#L266-L276)
```python
def compare(pto_grad_w, bm_grad_w, golden_grad_w):
    result, mare_matrix, mere_matrix, rmse_matrix, small_value_matrix = precision_compare_triple(
        pto_grad_w, bm_grad_w, golden_grad_w)
    ...
    if result != "PASS":
        raise Exception("fail precision check")
```

#### 4.3.4 代码实践

1. **实践目标**：用手工构造的小张量验证判定逻辑，确认你对比值计算的理解。
2. **操作步骤**（需 torch_npu 已安装，CPU 即可）：
   ```python
   import sys; sys.path.append("./ops-nn/quant/ai_infra_pypto_qat")
   import torch
   from tests.utils import precision_compare_triple
   torch.manual_seed(0)
   golden = torch.randn(1000)                      # 拟 golden
   bm = golden + 0.001 * torch.randn(1000)          # 基准：1e-3 级噪声
   good = golden + 0.0015 * torch.randn(1000)       # kernel 略差于基准
   bad = golden + 0.5 * torch.randn(1000)           # kernel 明显劣化
   print(precision_compare_triple(good, bm, golden)[0])   # 预期 PASS
   print(precision_compare_triple(bad, bm, golden)[0])    # 预期 FAILED
   ```
3. **需要观察的现象**：第一组四个比值都在 1 附近（good 与 bm 噪声同量级）；第二组 mare/mere 比值远超 2/1.2。
4. **预期结果**：`PASS`、`FAILED` 各一次； FAILED 场景若直接走 `compare()` 会抛出 `fail precision check`。
5. 具体比值数值**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 MARE 用 max（最坏情况）而 MERE 用 mean（平均）？只有 MERE 卡住、MARE 超标说明什么？

**答案**：mean 反映整体精度水平，max 反映最坏单点。若 mere 达标而 mare 超标，说明绝大多数元素很准、但存在个别离群点（往往是某个边界分支、尾块或溢出处理出错）——这时应去查特殊路径而非整体算法；反之 mere 也超标则是系统性误差（公式或 dtype 链路错了）。两者搭配把「整体」与「个别」两类 bug 分开。

**练习 2**：小值域用「计数比」而不是相对误差，`max(bm_error_count, 1)` 中的 1 是防什么？

**答案**：防止 0 除。当基准 bm 在小值域一次错都没有（cnt_bm=0）时，kernel 哪怕只错 1 个元素，比值也会变成无穷大而误判；取 max(...,1) 后该比值退化为 kernel 的绝对错误个数，只要 ≤2 仍可接受。这是「分母下限钳制」，与 `compute_re_matrix` 中 `max(bm_value, small_value_thres)` 同一思想。

### 4.4 两个通用驱动：forward_test 与 backward_test_autograd

#### 4.4.1 概念说明

`forward_test` 和 `backward_test_autograd` 是所有用例共用的执行引擎，它们把「三方对比」的编排固化下来：

- **inputs**：golden 侧输入（只有张量）；
- **pto_inputs**：kernel 侧输入（张量 + 标量，顺序与 wrapper 签名一致）；
- **golden_func**：参考实现闭包，末位参数必须是 `is_golden`——`False` 时按 FP32 跑（当 benchmark），`True` 时按 FP64 跑（当 golden）。**一份代码，两种角色**，保证 bm 与 golden 公式永不漂移。

反向驱动则额外用 torch autograd 造出「真实训练场景」的梯度：对 benchmark 与 golden 分别挂计算图、用同一组随机 grad_outputs 反传，再与 kernel 反向输出逐输入对比。

#### 4.4.2 核心流程

前向驱动：

```text
forward_test(inputs, pto_inputs, golden_func, pto_func)
  ├─ bm_inputs  = clone_inputs(inputs)            # 保 requires_grad 的克隆
  ├─ golden_inputs = to_double_inputs(inputs)     # cpu + float64
  ├─ bm_out    = golden_func(*bm_inputs,  is_golden=False)   # NPU FP32 基准
  ├─ golden_out= golden_func(*golden_inputs,is_golden=True)  # CPU FP64 金标准
  ├─ kernel_out= pto_func(*pto_inputs)                       # 被测 kernel
  └─ 逐输出：assert_allclose(仅记录) + compare(kernel, bm, golden)（硬判定）
```

反向驱动：

```text
backward_test_autograd(inputs, pto_inputs, golden_func, pto_func)
  ├─ bm：golden_func(is_golden=False) → 输出转 BF16 → randn 造 grad_outputs
  │      → torch.autograd.backward → collect_grads(bm_grads)
  ├─ golden：输入转 cpu/double → 前向 → grad_outputs 升 FP64 → backward
  ├─ kernel：pto_func(grad_outputs..., *pto_inputs)   # 反向 wrapper 第一个参数是 grad_output
  └─ 对每个 requires_grad 的输入 i：compare(pto_grads[idx], bm_grads[i], golden_grads[i])
```

#### 4.4.3 源码精读

前向驱动的三路执行——注意 `is_golden` 开关如何切换同一函数的两种精度身份：

[tests/utils.py:L359-L367](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py#L359-L367)
```python
bm_inputs = clone_inputs(inputs)
golden_inputs = to_double_inputs(inputs)

bm_out = normalize_outputs(golden_func(*bm_inputs, is_golden=False))
golden_out = normalize_outputs(golden_func(*golden_inputs, is_golden=True))
kernel_out = normalize_outputs(pto_func(*pto_inputs))
```

`assert_allclose` 只记录不判死（try/except 吃掉异常、仅 `logger.error`），真正的守门员是下一行的 `compare`：

[tests/utils.py:L374-L383](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py#L374-L383)
```python
try:
    assert_allclose(kernel_out[i].float().cpu(), bm_out[i].float().cpu(), rtol=1e-3, atol=1e-3)
except Exception as e:
    logger.error(e)
result = compare(kernel_out[i], bm_out[i], golden_out[i])
```
初读容易误以为 rtol=1e-3/atol=1e-3 是判据；它是诊断信息（超限时打一条日志帮助定位），失败与否只由 `compare` 的三比值决定。若你在日志里看到 assert_allclose 的 error 但测试仍 PASS，不是 bug，是设计。

反向驱动的 benchmark 段——输出先转 BF16 再造梯度，模拟真实训练里激活是低精度的事实：

[tests/utils.py:L399-L413](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py#L399-L413)
```python
bm_out = golden_func(*bm_inputs, is_golden=False)
bm_out = normalize_outputs(bm_out)
bm_out = tuple(o.to(torch.bfloat16) for o in bm_out)
grad_outputs = tuple(torch.randn(o.shape, device=o.device, dtype=o.dtype) for o in bm_out)
torch.autograd.backward(bm_out, grad_outputs)
bm_grads = collect_grads(bm_inputs)
```

kernel 反向的调用约定——单输出时 grad_outputs 展开为首参，其余照抄 pto_inputs；这正对应 u7-l3 读过的反向 wrapper 签名 `ai_infra_qat_symmetric_per_channel_backward(grad_output, weight, scale, eps, min_v, max_v)`（[op_code/ai_infra_pypto_qat.py:L379-L393](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L379-L393)）：

[tests/utils.py:L438-L444](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py#L438-L444)
```python
if len(grad_outputs) == 1:
    pto_grads = pto_func(grad_outputs[0], *pto_inputs)
else:
    pto_grads = pto_func(*grad_outputs, *pto_inputs)
```

梯度收集的防御——某输入声明了 requires_grad 却没收到梯度，直接报错而不是拿 None 去比：

[tests/utils.py:L320-L332](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py#L320-L332)
```python
for x in inputs:
    if isinstance(x, torch.Tensor) and x.requires_grad:
        if x.grad is None:
            raise Exception(f"[ERROR]grad is None, shape={tuple(x.shape)}")
```

对比循环按「requires_grad 的输入」对齐三路梯度，kernel 侧用独立下标 idx 递增——这要求反向 wrapper 的多输出顺序与输入张量顺序一致：

[tests/utils.py:L450-L466](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py#L450-L466)
```python
for i, x in enumerate(inputs):
    if isinstance(x, torch.Tensor) and x.requires_grad:
        pto_grad = pto_grads[idx]
        bm_grad = bm_grads[i]
        golden_grad = golden_grads[i]
        ...
        result = compare(pto_grad, bm_grad, golden_grad)
        idx += 1
```

#### 4.4.4 代码实践

1. **实践目标**：以 `forward_test` 为对象画数据流图，核对每个箭头的 dtype/device。
2. **操作步骤**：
   - 对照上面四段源码，手画三列泳道图（benchmark / golden / kernel），标注每列的输入预处理（clone 保梯度 / cpu+double / 原样+标量）、执行体、输出 dtype（BF16 / FP64 / BF16）。
   - 回答：`bm_out` 与 `kernel_out` 都在 NPU 上、`golden_out` 在 CPU 上，`compare` 内部如何抹平这个差异？（提示：看 [tests/utils.py:L237-L239](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py#L237-L239)，三方都先 `.to(torch.float32)` 再 `.cpu()`。）
3. **需要观察的现象**：图中 golden 泳道唯一全程不碰 NPU；两处「BF16→FP32」的升精度都发生在比较之前、计算之后——精度损失留在被测路径里，比较路径保持高精度。
4. **预期结果**：一张三泳道图 + 对「比较前统一升 FP32、搬 CPU」的解释。
5. 纯读码任务，无需运行环境。

#### 4.4.5 小练习与答案

**练习 1**：`backward_test_autograd` 里 golden 一侧为什么把 grad_outputs 也 `detach().cpu().double()`，而不是直接用 BF16 的原版？

**答案**：golden 是金标准，它的整条链路（输入、权重、梯度上游）都应在最高精度下运行，否则 FP64 前向配 BF16 梯度会让「金标准」自身携带 BF16 噪声，三比值里分母（golden 与 bm 的差）就不纯净了。BM 侧保留 BF16 梯度则是刻意还原真实训练；kernel 侧输入本来就是 BF16。三路各按自己的「人设」取精度，对比才有意义。

**练习 2**：如果反向 wrapper 的输出顺序写反了（先 grad_scale 后 grad_weight），这个驱动会怎样表现？

**答案**：不会报 shape 错——per_channel 下 grad_weight 是 (N,M)、grad_scale 是 (N,1)，形状不同，`precision_compare_triple` 内的张量运算会因广播或维度不匹配报错；但在两个输出同形的算子里（如多路等形梯度）会**静默比错对象**，得出无法解释的精度结果。所以驱动隐含一条契约：反向 wrapper 的输出顺序 = 输入张量声明顺序中 requires_grad 者的顺序。

### 4.5 用例文件解剖：golden 闭包与参数化设计

#### 4.5.1 概念说明

本模块把前四个模块的零件装配起来，解剖三个代表性用例文件：symmetric per_channel 前向（最简样本）、它的反向（autograd 驱动样本）、asymmetric per_group 前向（带校验的复杂 golden）。你将看到用例文件本身很薄——所有重活都在 utils.py，用例只提供「公式 + 形状 + 参数表」。

#### 4.5.2 核心流程

写一个新用例的五步法：

1. **写 golden 工厂**：闭包捕获标量，函数体逐行翻译 docs 公式；`is_golden` 分支只在输入 dtype 上分叉（FP32 vs FP64），公式本体共用。
2. **写 run_single_test**：由 (N, M, bit...) 推导 min_v/max_v、scale/offset 形状，`create_input` 造数，组装 inputs / pto_inputs 两个列表。
3. **填参数化表**：shape 必须满足 docs 约束（per_channel 要求 M∈[128,3072] 且被 128 整除，见 [docs/qat_ops.md:L354-L356](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/docs/qat_ops.md#L354-L356)）。
4. **选驱动**：前向用 `forward_test`，反向给输入挂 `requires_grad_(True)` 后用 `backward_test_autograd`。
5. **（可选）开 CSV**：utils 顶部 `collect_result = False` 改 True，结果追加写入 csv 供回归对比。

#### 4.5.3 源码精读

**样本一：symmetric per_channel 前向。** golden 工厂的核心五行——protected scale、归一化、STE round、clamp、反量化，与 u7-l2 讲的公式链逐条对应：

[tests/st/test_ai_infra_qat_symmetric_per_channel.py:L35-L51](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/st/test_ai_infra_qat_symmetric_per_channel.py#L35-L51)
```python
if not is_golden:
    weight_in = weight.float()          # benchmark 身份：FP32
    scale_in = scale.float()
else:
    weight_in = weight.to(torch.float64)  # golden 身份：FP64
    scale_in = scale.to(torch.float64)
eps_tensor = torch.tensor(eps, device=weight.device, dtype=torch.float64)
protected_scale = torch.where(scale_in > eps_tensor, scale_in, eps_tensor)
weight_normalized = weight_in / protected_scale
weight_rounded = (weight_normalized.round() - weight_normalized).detach() + weight_normalized
clamped = torch.clamp(weight_rounded, min_v, max_v)
output = clamped * protected_scale
```
第 44 行就是 STE：`(round(x) - x).detach() + x` 前向值等于 x（round 差被加回），反向梯度因 detach 直通——golden 用 torch autograd 自动获得与 kernel 相同的直通语义，无需手写梯度。

`run_single_test` 的组装——两个列表的差集就是标量参数：

[tests/st/test_ai_infra_qat_symmetric_per_channel.py:L56-L68](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/st/test_ai_infra_qat_symmetric_per_channel.py#L56-L68)
```python
min_v = float(-2**(bit-1))
max_v = float(2**(bit-1) - 1)
weight_shape = (N, M)
scale_shape = (N, 1)
weight = create_input(weight_shape, torch.bfloat16, device, distribution, seed)
scale = create_input(scale_shape, torch.bfloat16, device, distribution, seed)
golden_inputs = [weight, scale]
pto_inputs = [weight, scale, eps, min_v, max_v]
golden = create_symmetric_qat_nscale_golden(eps, min_v, max_v)
return forward_test(golden_inputs, pto_inputs, golden, ai_infra_qat_symmetric_per_channel)
```
`pto_inputs` 的顺序严格对齐 wrapper 签名 `ai_infra_qat_symmetric_per_channel(weight, scale, eps, min_v, max_v)`（[op_code/ai_infra_pypto_qat.py:L284-L295](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L284-L295)）——u7-l1 讲过「参数顺序是硬契约」，测试侧同样如此。

**样本二：symmetric per_channel 反向。** 与前向文件几乎逐行相同，只有两处差异——输入挂梯度、换驱动：

[tests/st/test_ai_infra_qat_symmetric_per_channel_backward.py:L63-L68](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/st/test_ai_infra_qat_symmetric_per_channel_backward.py#L63-L68)
```python
weight = create_input(weight_shape, torch.bfloat16, device, distribution, seed).requires_grad_(True)
scale = create_input(scale_shape, torch.bfloat16, device, distribution, seed).requires_grad_(True)
inputs = [weight, scale]
pto_inputs = [weight, scale, eps, min_v, max_v]
golden = create_symmetric_qat_nscale_golden(eps, min_v, max_v)
return backward_test_autograd(inputs, pto_inputs, golden, ai_infra_qat_symmetric_per_channel_backward)
```
golden 闭包**原样复用**前向文件的实现（两文件各有一份相同拷贝）——反向正确性的参照就是「前向公式的 autograd」，torch 自动对 STE 闭包求导，bm 与 golden 的梯度因此也无需手写。

**样本三：asymmetric per_group 前向。** golden 先做三重 shape 校验（2 维、M 整除 group_size、scale/offset 形状为 (num_groups,1)），把非法配置挡在造数阶段：

[tests/st/test_ai_infra_qat_asymmetric_per_group.py:L39-L55](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/st/test_ai_infra_qat_asymmetric_per_group.py#L39-L55)
```python
if weight_in.ndim != 2:
    raise ValueError(f"weight must be 2D (n, m), got shape {tuple(weight.shape)}")
if weight_in.shape[1] % group_size != 0:
    raise ValueError(...)
...
num_groups = weight.numel() // group_size
expected_group_shape = (num_groups, 1)
if tuple(scale.shape) != expected_group_shape:
    raise ValueError(...)
```

公式链本体——u7-l3 的九步公式在 torch 里的直译（`view(num_groups, group_size)` 完成分组、`n_levels=2^(bit-1)`、`shift=0.5`）：

[tests/st/test_ai_infra_qat_asymmetric_per_group.py:L57-L71](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/st/test_ai_infra_qat_asymmetric_per_group.py#L57-L71)
```python
weight_2d = weight_in.view(num_groups, group_size)
n_levels = 2 ** (bit - 1)
shift = 0.5
weight_shifted = weight_2d - offset_in
alpha = protected_scale * n_levels
weight_clipped = torch.clamp(weight_shifted / alpha, -clip_val, clip_val) * n_levels - shift
weight_rounded = (weight_clipped.round() - weight_clipped).detach() + weight_clipped
weight_unshifted = weight_rounded + shift
weight_denorm = weight_unshifted / n_levels
output_2d = weight_denorm * alpha + offset_in
```

其 `run_single_test` 展示了第三种粒度的形状推导——分组数 = 行数 × 每行组数：

[tests/st/test_ai_infra_qat_asymmetric_per_group.py:L84-L95](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/st/test_ai_infra_qat_asymmetric_per_group.py#L84-L95)
```python
weight_shape = (N, M)
groups_per_row = M // group_size
num_groups = N * groups_per_row
scale_shape = (num_groups, 1)
offset_shape = (num_groups, 1)
...
pto_inputs = [weight_bm, scale_bm, offset_bm, group_size, bit, eps, clip_val]
```

#### 4.5.4 代码实践（本讲主实践）

1. **实践目标**：为 symmetric per_channel 前向新增一条 `N=512、M=768、scale 逐通道随机` 的用例，断言与 CPU 参考实现的误差在阈值内。
2. **操作步骤**：
   - 合法性核对：`M=768 = 128×6`，满足 [docs/qat_ops.md:L356](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/docs/qat_ops.md#L356) 的「M∈[128,3072] 且被 128 整除」；N 为首维动态维度，无约束。
   - 修改 [tests/st/test_ai_infra_qat_symmetric_per_channel.py:L71-L80](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/st/test_ai_infra_qat_symmetric_per_channel.py#L71-L80) 的参数表，追加一行（示例代码）：
     ```python
     for test in [
         (153376, 2048, 4, 0.0001),
         (38344, 2048, 4, 0.0001),
         (512, 768, 4, 0.0001),     # 新增：小 shape + M=768=6*128
     ]
     ```
     说明：`scale_shape=(N,1)=(512,1)`，`create_input` 的 uniform_large 本就逐通道独立随机——512 个通道各取 [-5,5] 的随机值，「逐通道随机」由现有造数天然满足，无需额外代码。
   - 运行（在 pypto/src 下）：
     ```bash
     pytest "ops-nn/quant/ai_infra_pypto_qat/tests/st/test_ai_infra_qat_symmetric_per_channel.py" \
            -k "N512-M768" -x -s
     ```
3. **需要观察的现象**：日志依次打出 `=== Forward Output[0] ===`、`pto_data.dtype=torch.bfloat16` 与四个比值（mare/mere/rmse/small_value）及 `precision result: PASS`。
4. **预期结果**：用例通过，四个比值均 ≤ 阈值。
5. **阈值取值依据（含「待确认」声明）**：本框架不用绝对 atol/rtol 做最终判据，判定阈值来自 [utils.py:L216-L220](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py#L216-L220)——默认 `thres=(2, 1.2, 1.2)` 即昇腾精度标准 2.1 的 **L2 关键算子级**，加小值域计数比 ≤2（BF16 划分阈值 \(2^{-8}\)、错误阈值 \(2^{-16}\)，见 [utils.py:L149-L163](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py#L149-L163)）。为何 QAT 算子按 L2 而非 L0/L1，仓库未写明，属隐含约定（推测因其直接作用于权重梯度通路），**待确认**；若新用例仅求与既有用例一致，沿用默认即可，无需改任何阈值。本环境无 NPU，运行结果**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：把新用例的 bit 从 4 改成 8（min_v=-128、max_v=127，经典 INT8 档），精度结论预期如何变化？需要改哪些地方？

**答案**：只需在参数表加 `(512, 768, 8, 0.0001)` 一行——min_v/max_v 由 `run_single_test` 从 bit 自动推导。bit=8 时量化格点更密（\(2^8\) 级 vs \(2^4\) 级），同分布输入下 clamp 触发更少、伪量化重建误差更小，预期三比值更宽松地通过；但它同时改变了 round 的舍入粒度，属于值得单独跑一档的参数维度。

**练习 2**：为什么 asymmetric golden 里要做三重 shape 校验，而 symmetric 的 golden 一行校验都没有？

**答案**：symmetric per_channel 的 scale 形状 (N,1) 与 weight 首维天然对齐，shape 错误会在张量广播处直接报错，torch 替你当了守门员；asymmetric per_group 的正确形状 (num_groups,1) 无法从 weight 形状一眼推出（依赖 group_size 参数），一旦传错可能被广播「无声吞掉」算出错误答案——所以必须在计算前显式校验。原则：**凡是 torch 广播能默默容忍的 shape 错误，golden 都要自己拦下**，否则金标准本身就是错的，三方对比全部失效。

**练习 3**：前向用例的 `test_model` 里有 `if collect_result:` 分支写 CSV，它和精度判定是什么关系？

**答案**：完全正交。CSV（如 `symmetric_qat_nscale_model.csv`）只是把每个用例的参数与四个比值追加落盘，供跨版本回归对比、观察精度漂移趋势；判定仍由 `compare` 抛异常完成。`collect_result` 是 utils.py 模块级开关（[utils.py:L21](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py#L21)），默认关闭，手工改 True 后重跑才生效。

## 5. 综合实践

**任务**：给 symmetric per_channel 建「分布敏感性报告」，把本讲四个模块串起来。

1. 在 `pypto/src` 下，把 [utils.py:L24-L32](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py#L24-L32) 的 `DISTRIBUTION` 临时改为五种全开（`uniform_large`、`uniform_small`、`uniform[-10,10]`、`normal`、`outlier`），并按 4.5.4 加上 `N=512, M=768` 用例。
2. 把 [utils.py:L21](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py#L21) 的 `collect_result` 改为 True，运行：
   ```bash
   pytest ops-nn/quant/ai_infra_pypto_qat/tests/st/test_ai_infra_qat_symmetric_per_channel.py -k "N512-M768"
   ```
3. 打开生成的 `symmetric_qat_nscale_model.csv`，按分布整理一张表：每个分布的 mare/mere/rmse/small_value 四个比值。
4. 分析三个问题：(a) 哪个分布的四比值最高、为什么？(b) `uniform_small` 下小值域计数比是否明显变化？(c) `outlier` 是否主要推高 MARE（个别离群点）而非 MERE（整体）？对照 4.3 练习 1 的「max vs mean」诊断法给出结论。
5. 跑完把 `DISTRIBUTION` 与 `collect_result` 还原，避免把临时配置带进仓库。
6. 本环境无 NPU，全部运行结果**待本地验证**；无 NPU 时至少完成参数表与分布表的两处代码编辑（用 `python -m py_compile` 或 `pytest --collect-only` 验证语法与收集）。

## 6. 本讲小结

- pypto 的 st 测试是**自包含**的：`tests/utils.py` 一个文件承载造数（`create_input` 五种分布、固定 seed 可复现）、精度判定（双值域 + 三比值）、两个通用驱动（`forward_test` / `backward_test_autograd`），六个用例文件只是「golden 公式 + 参数表」的薄壳，构成前反向 × 三粒度的完整矩阵。
- 精度判定采用**三方对比**：kernel（NPU BF16）对 benchmark（NPU FP32 诚实实现）对 golden（CPU FP64）；判据不是绝对 atol/rtol，而是 kernel 误差相对 benchmark 误差的倍数——大值域看 MARE/MERE/RMSE 三比值（默认 L2 级 2/1.2/1.2），小值域看错误计数比（≤2），`assert_allclose(1e-3)` 仅是诊断日志不是判据。
- golden 用 `is_golden` 开关一份代码演两种角色，bm 与 golden 公式永不漂移；STE 通过 `(round(x)-x).detach()+x` 写进 golden，反向梯度由 torch autograd 自动获得，无需手推。
- 用例侧两条硬契约：`pto_inputs` 顺序对齐 wrapper 签名；反向 wrapper 多输出顺序对齐 requires_grad 输入顺序。运行必须从 `pypto/src` 目录启动（`sys.path.append` 相对路径），设备由 `TILE_FWK_DEVICE_ID` 指定。
- 与 ascendc 的 st 框架（u8-l3 将展开）相比：pypto 每包自带轻量工具箱、三方比值判定；ascendc 集中在 `ascendc/src/tests/st`（conftest.py + pytest.ini）、以 torch_npu.testing.testcase 为基类做 NPU 对 CPU 高精度 golden 的两方对比。两套框架共享同一套「MARE/MERE/RMSE + L0/L1/L2 + 小值域容差」的昇腾精度标准。

## 7. 下一步学习建议

本讲完成后，第 7 单元（pypto 算子开发）全部结束。两条后续路线：

1. **横向对比测试框架**：进入第 8 单元，从 [u8-l1 UT 框架 framework_normal 总览](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/common/tiling_context_faker.h) 读起，重点在 u8-l3 对照 ascendc 的 st 体系（`ascendc/src/tests/st/` 下的 conftest.py 与 pytest.ini）回看本讲的 pypto 框架，体会「同一精度标准、两套工程化封装」。
2. **纵向深挖被测对象**：若想继续在量化领域深入，重读 [op_code/ai_infra_pypto_qat.py](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py) 中尚未精读的 per_tensor 前反向 kernel，并亲手为它们补一条 `uniform_small` 分布用例——那是检验你是否真正掌握本讲框架的最好试金石。
