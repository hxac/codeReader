# ST 精度测试：MARE/MERE/RMSE 与精度分级

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 ST（系统测试）与 UT（单元测试）在本仓库中的分工边界：UT 在宿主机上验证流程与分支，ST 在真机上验证数值精度。
2. 手算并解释 MARE / MERE / RMSE 三个误差指标的含义与互补作用。
3. 解释 L0 / L1 / L2 三档精度等级的「比值判定」思想——为什么不拿 NPU 输出与 golden 的绝对误差直接比阈值。
4. 解释小值域（small value domain）特殊容差的动机，并能说明 bfloat16 中 `threshold=2^-8`、`error=2^-16` 各自的含义。
5. 独立编写一个带 CPU golden 对比的 st 用例，并理解 `conftest.py` / `pytest.ini` 如何用 `resources` marker 筛选真机用例。

## 2. 前置知识

本讲默认你已完成 u6-l1（torch_ops_extension 总览）与 u8-l1/u8-l2（UT 框架）。用到的基础概念如下：

- **ST（System Test，系统测试）**：把算子当成黑盒，在真实 NPU 上喂真实数据，比对输出数值是否「足够接近」参考实现。它与 UT 的关系是：UT 回答「流程对不对、分支走没走对」，ST 回答「数算得准不准」。
- **golden（金标准）**：一份理论上正确的输出。本仓库的做法是把同一份输入放到 CPU 上、升到 float64 精度重新算一遍，得到 golden——CPU 高精度实现被视为「最接近真值」的参考。
- **标杆实现（benchmark）**：golden 之外的第二参考，通常是「同一精度（如 bf16）下的另一条实现路径」。本讲会看到，aggregate_hidden 的 st 用例里名为 `gpu` 的变量其实是 **NPU 上用 torch 原生 Conv1d（Cube 路径）算的标杆**，并不是 CUDA GPU。
- **torch_npu.testing.testcase**：torch_npu 提供的测试基类，继承自 `unittest.TestCase` 并附带 NPU 环境初始化与张量断言工具，是仓库所有 st 用例的公共底座。
- **pytest marker 与 conftest**：pytest 允许在用例上打标记（marker），并在 `conftest.py` 的收集钩子里按标记筛选用例。本仓库用 `resources` marker 声明用例需要的硬件资源。
- **bf16 的精度轮廓**：bfloat16 是 1 位符号 + 8 位指数 + 7 位显式尾数（有效尾数 8 位），指数位与 fp32 相同、尾数位远少于 fp32。它的相对分辨率约为 \(2^{-8}\)，这个数字在本讲的小值域配置里会再次出现。
- **相对误差在零附近的爆炸**：\( |a-g|/|g| \) 当 \( g \to 0 \) 时趋向无穷。一个绝对误差只有 \( 10^{-6} \) 的点，如果 golden 恰好是 \( 10^{-8} \)，相对误差就是 100 倍。这就是需要「小值域单独处理」的数学根源。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/tests/st/test_ai_infra_aggregate_hidden.py` | 本讲主标本：一个完整的 st 用例，自带 MARE/MERE/RMSE 统计、L0/L1/L2 分级与小值域容差的整套判定框架 |
| `ascendc/src/tests/st/conftest.py` | st 公共 pytest 插件：注册 `--device`/`--nodes`/`--npus-per-node` 选项，并按 `resources` marker 筛选（deselect）用例 |
| `ascendc/src/tests/st/pytest.ini` | 注册 `resources` marker 的声明，避免 unknown marker 告警 |
| `ascendc/src/ops-transformer/attention/flash_attention_score_enhance/tests/ut/op_api/test_aclnn_flash_attention_score_enhance.cpp` | 对照件：op_api 层 UT 的写法（TensorDesc + OP_API_UT 宏 + 只测 GetWorkspaceSize），用来说明 UT 替代不了 ST |

另有环境事实两则：`ascendc/src/tests/requirements.txt` 只声明了 `tensorflow==2.20.0`，而 st 用例实际 import 了 `pandas`、`einops` 等未声明的依赖——st 环境需要手工补齐；仓库内共有 20 个 `tests/st/*.py` 用例分布在各算子目录下（可用 `find ascendc/src -path '*tests/st*' -name '*.py'` 复核）。

## 4. 核心概念与源码讲解

### 4.1 ST 用例骨架：torch_npu.testing.testcase 与「三方对比」结构

#### 4.1.1 概念说明

一个 st 用例要回答的问题是：「自定义算子（`torch.ops.custom.npu_aggregate_hidden`）在真机上的输出，和正确答案差多远？差的程度能不能接受？」

直接拿 NPU 输出与 golden 比绝对误差是行不通的——阈值定多少才算合格？bf16 本身就有约 \(2^{-8}\) 的相对分辨率，任何实现都逃不开格式本身的量化噪声。本仓库的答案是**三方对比**：

1. **golden**：CPU + float64 升精度的计算结果，代表「真值」；
2. **npu_out**：被测自定义算子的输出（bf16，真机）；
3. **gpu_out（标杆）**：同 dtype 的另一条实现路径（这里是 NPU 上 torch 原生 `Conv1d`），代表「该精度下的合理水平」。

判定的不是「npu 离 golden 多远」，而是「npu 离 golden 的距离，是否控制在标杆离 golden 距离的若干倍以内」。这个「若干倍」就是后面 L0/L1/L2 的档位值。

#### 4.1.2 核心流程

一个 st 用例的执行流程（对照 [test_ai_infra_aggregate_hidden.py:255-300](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/tests/st/test_ai_infra_aggregate_hidden.py#L255-L300)）：

```text
import torch / torch_npu / omni_training_custom_ops   ← 依赖 u6-l1 的 wheel 与 u1-l4 的算子包
        │
固定随机种子 np.random.seed(54)
        │
造数：x_cpu (bf16, requires_grad) + Conv1d 权重 normal_(1.0, 0.01)
        │
三路分身：
  ├─ x_npu / weight_npu      → torch.ops.custom.npu_aggregate_hidden   （被测对象）
  ├─ x_gpu / merge_conv_gpu  → NPU 上原生 Conv1d                         （标杆）
  └─ x_cpu / conv_cpu        → 升 float64 后重算                         （golden）
        │
precision_check(npu_out, gpu_out, golden, dtype)
  ├─ 正常域（|golden| ≥ threshold）：MARE/MERE/RMSE 比值 ≤ L2 档位值
  └─ 小值域（|golden| < threshold）：坏点个数比 ≤ 2.0
        │
assert result == True
```

#### 4.1.3 源码精读

先看 import 与全局开关（[test_ai_infra_aggregate_hidden.py:9-23](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/tests/st/test_ai_infra_aggregate_hidden.py#L9-L23)）：引入 `omni_training_custom_ops`（u6-l1 安装的 wheel，import 瞬间会初始化 aclnn 符号查找路径），并从 `torch_npu.testing.testcase` 取 `TestCase` 与 `run_tests`；第 23 行 `torch_npu.npu.config.allow_internal_format = True` 允许 torch_npu 使用内部排布格式（如 NZ）以获得性能，这会真实影响标杆路径的计算形态。注意 `pandas/pathlib/re`（第 19-21 行）在本文件中并未使用——这类残留提示该文件兼作其他 st 用例的模板。

用例主体（[test_ai_infra_aggregate_hidden.py:256-300](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/tests/st/test_ai_infra_aggregate_hidden.py#L256-L300)）值得逐段看：

- 第 256-257 行：两个 pytest marker——`filterwarnings` 压掉弃用告警，`resources(device="npu:*", npus_per_node=1)` 声明硬件需求（4.4 节会讲 conftest 如何消费它）。
- 第 258-264 行：`B=4, S=4096, H=768`（满足算子约束 H 为 192 的倍数、B≤8），`np.random.seed(54)` 固定种子保证可复现。
- 第 267-273 行：CPU 侧造数，权重用 `normal_(mean=1.0, std=0.01)` 初始化——从效果看，这让输出量级落在 \(O(1)\)，避免大量元素掉进小值域使正常域样本不足。
- 第 276-284 行：三路分身。关键在第 280 行 `weight_npu = merge_conv_npu.weight.transpose(0, 2).squeeze(1)`，把 torch 的 `[H,1,3]` 权重转成算子要的 `[3,H]`（即 `[W,H]`，对应 u2-l1 读到的权重约束）；以及第 282-284 行注释 `# gpu: Cube`——**变量名叫 gpu，实际是 `.to("npu")`**，即 NPU 上走 Cube 矩阵乘的原生 Conv1d 标杆。
- 第 287-288 行：CPU 路径升精度到 float64 再计算，产出 golden。
- 第 292-296 行：三路各算一次。第 299 行 `precision_check(conv_out_npu, conv_out_gpu, conv_out_cpu.npu(), dType)` 把 golden `.npu()` 搬到设备上，保证后续逐元素运算三方同设备。
- 第 298 行注释「以精度2.1标准去做对比」是团队内部的精度标准命名；代码里实际生效的是 `precision_levels_config` 与 `DEFAULT_PRECISION_LEVEL = "L2"`。

golden 的产生逻辑在 [test_ai_infra_aggregate_hidden.py:225-252](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/tests/st/test_ai_infra_aggregate_hidden.py#L225-L252)：`aggregate_hidden_torch` 用零填充实现因果窗口（S 首维转成 Conv1d 要的末维）、分组卷积（`groups=H`，通道独立）、可选 mask 置零，最后转回 `[S,B,H]`。这与 u2-l1 从 README 读到的公式完全对应。

还有一个容易忽略的细节：文件末尾**没有** `if __name__ == "__main__": run_tests()` 入口（文件在第 300 行结束）。也就是说虽然 import 了 `run_tests`，这个用例是给 pytest 收集执行的，不是 `python test_xxx.py` 直跑的——method 上的 pytest marker 也印证了这一点。

#### 4.1.4 代码实践

**实践目标**：不改任何代码，纯靠阅读画出一个 st 用例的「数据流三叉图」。

**操作步骤**：

1. 打开 [test_ai_infra_aggregate_hidden.py:255-300](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/tests/st/test_ai_infra_aggregate_hidden.py#L255-L300)，在纸上画出 `x_cpu` 分裂成 `x_npu / x_gpu / x_cpu(float64)` 三条支路。
2. 在每条支路上标注：设备（npu/cpu）、dtype（bfloat16/float64）、计算入口（`torch.ops.custom.npu_aggregate_hidden` / 原生 Conv1d / 原生 Conv1d）、产出的角色（被测/标杆/golden）。
3. 用 `grep -n "run_tests\|__main__"` 检查该文件是否有 main 入口，验证「pytest 收集」的判断。

**需要观察的现象**：三支路共用同一份种子与同一份初始权重（`copy.deepcopy` 避免原地搬家污染），只有精度与实现路径不同。

**预期结果**：三叉图中被测与标杆同为 bf16+NPU，唯一差异是实现；golden 独占 CPU+float64。（本实践为纯阅读型，无需运行环境。）

#### 4.1.5 小练习与答案

**练习 1**：st 用例里名为 `gpu_out` 的张量到底在什么设备上、由什么算出？为什么要它存在？

**答案**：在 NPU 上（`x_gpu = x_cpu.to("npu")`，见第 283 行），由 torch 原生 `Conv1d`（Cube 路径）算出。它存在的意义是提供「同精度下的合理误差水平」作分母，使判定变成相对比值而不是绝对阈值。

**练习 2**：为什么 golden 必须用 float64 而不是同样用 bf16 算？

**答案**：golden 的职责是逼近真值。bf16 只有 8 位有效尾数，自己就带约 \(2^{-8}\) 的相对量化误差，拿它当真值会把「格式噪声」错当「实现误差」，比值判定的分母就失去意义。

**练习 3**：这个用例为什么没有 `run_tests()` 的 main 入口也能被执行？

**答案**：它继承 `torch_npu.testing.testcase.TestCase`（即 `unittest.TestCase` 子类），方法名以 `test_` 开头，配合 pytest marker，由 pytest 按 unittest 风格收集执行。

### 4.2 三把误差尺子：MARE / MERE / RMSE 与统一统计接口

#### 4.2.1 概念说明

给定被测输出 \( a \) 与 golden \( g \)（同 shape、g 为 float64），先定义两个基础量：

- 绝对误差：\[ \mathrm{AE}_i = |a_i - g_i| \]
- 相对误差：\[ \mathrm{RE}_i = \frac{|a_i - g_i|}{|g_i| + \varepsilon}, \quad \varepsilon = 10^{-7} \]

分母加 \( \varepsilon \) 是防零保护。在此之上定义三个汇总指标：

\[ \mathrm{MARE} = \max_i \mathrm{RE}_i \qquad \mathrm{MERE} = \frac{1}{n}\sum_i \mathrm{RE}_i \qquad \mathrm{RMSE} = \sqrt{\frac{1}{n}\sum_i (a_i - g_i)^2} \]

三把尺子各管一段：

| 指标 | 中文名 | 敏感对象 | 盲区 |
| --- | --- | --- | --- |
| MARE | 最大相对误差 | 单个最坏的点（离群误差） | 平均水平好时也可能被一个点打爆 |
| MERE | 平均相对误差 | 整体相对偏差水平 | 掩盖个别坏点 |
| RMSE | 均方根误差 | 绝对幅度上的整体偏差（放大大误差） | 有量纲，且对小值域点的相对失真不敏感 |

三者同时达标才算过关，等价于「既不许有个别坏点（MARE），也不许整体漂移（MERE），还不许绝对幅度跑偏（RMSE）」。

#### 4.2.2 核心流程

```text
actual, golden
   ├─ absolute_error  → |a-g|            （基础件）
   ├─ relative_error  → |a-g|/(|g|+eps)  （基础件）
   ├─ max_relative_error     → MARE
   ├─ mean_relative_error    → MERE
   ├─ root_mean_squared_error→ RMSE
   └─ calc_error_metrics      → {"MARE":…, "MERE":…, "RMSE":…}
        ↑ 断言 shape 相同、golden.dtype == float64（契约检查）
```

#### 4.2.3 源码精读

常量与配置（[test_ai_infra_aggregate_hidden.py:26-27](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/tests/st/test_ai_infra_aggregate_hidden.py#L26-L27)）：`EPS = 1e-7`、`GOLDEN_CPU_DTYPE = torch.float64`（注释「cpu golden升精度的位数」）。

两个基础件（[test_ai_infra_aggregate_hidden.py:47-58](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/tests/st/test_ai_infra_aggregate_hidden.py#L47-L58)）：`absolute_error` 逐元素取绝对差；`relative_error` 逐元素算 \( |a-g|/(|g|+\varepsilon) \)。

三个汇总函数（[test_ai_infra_aggregate_hidden.py:62-87](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/tests/st/test_ai_infra_aggregate_hidden.py#L62-L87)）：`max_relative_error` 取 `re.max().item()`；`mean_relative_error` 取 `re.mean().item()`；`root_mean_squared_error` 按 `diff*diff → mean → sqrt` 三步走。

统一出口（[test_ai_infra_aggregate_hidden.py:91-103](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/tests/st/test_ai_infra_aggregate_hidden.py#L91-L103)）：`calc_error_metrics` 先断言 `actual.shape == golden.shape` 和 `golden.dtype == GOLDEN_CPU_DTYPE`——把「golden 必须 float64」写成了硬契约，再把三指标打包成 dict 返回，供比值判定遍历。

#### 4.2.4 代码实践

**实践目标**：在纯 CPU 环境（无需 NPU、无需安装 wheel）手工验证三个指标函数的行为，建立数量直觉。

**操作步骤**：把 [test_ai_infra_aggregate_hidden.py:47-103](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/tests/st/test_ai_infra_aggregate_hidden.py#L47-L103) 的函数抄进一个独立脚本（示例代码，仅需 torch CPU）：

```python
# 示例代码：metrics_demo.py（CPU 即可运行）
import torch
golden = torch.tensor([1.0, 2.0, 4.0, 8.0], dtype=torch.float64)
actual = torch.tensor([1.1, 1.9, 4.2, 7.6], dtype=torch.float64)
print(calc_error_metrics(actual, golden))
```

**需要观察的现象**：输出 `{'MARE': ..., 'MERE': ..., 'RMSE': ...}`。

**预期结果**：AE = [0.1, 0.1, 0.2, 0.4]；RE ≈ [0.1, 0.05, 0.05, 0.05]；因此 MARE ≈ 0.1（严格说略小于，因分母含 \(+\varepsilon\)）、MERE ≈ 0.0625、RMSE = \(\sqrt{0.055}\) ≈ 0.2345。手算与脚本输出应一致到浮点舍入。

**实践目标 2**：构造一个「MARE 很大、MERE 很小」的反例（如 100 个点中 1 个错 10 倍、其余全对），观察 MARE 报警而 MERE 沉默——体会为什么必须三把尺子同时用。

#### 4.2.5 小练习与答案

**练习 1**：`relative_error` 的分母为什么加 `EPS=1e-7` 而不是直接除以 `|golden|`？

**答案**：golden 可能为 0（如被 mask 置零的位置），直接除会得到 inf/NaN，使后续 max/mean 失效。加一个小量做防零保护，同时 \( \varepsilon \) 相对正常量级的 \( |g| \) 可忽略，不扭曲结果。

**练习 2**：RMSE 与 MERE 的量纲有何不同？为什么比值判定能让它们同台比较？

**答案**：MERE 无量纲（同量纲相除），RMSE 带输出值的量纲。比值判定中两者都各自做 `npu/gpu` 的除法，量纲在比值中相消，因此三个指标可以共用同一套档位数值。

**练习 3**：`calc_error_metrics` 里为什么断言 `golden.dtype == torch.float64`？

**答案**：这是把「golden 必须由 CPU 升精度产生」的用例约定固化为代码契约，防止后人拿 bf16 的伪 golden 调用该框架，静默得出无意义的误差值。

### 4.3 正常域比值判定与 L0/L1/L2 精度分级

#### 4.3.1 概念说明

精度等级是三档「放大倍数」许可：

| 等级 | MARE ≤ | MERE ≤ | RMSE ≤ | 定位 |
| --- | --- | --- | --- | --- |
| L0 | 10.0 | 2.0 | 2.0 | 宽松（快速迭代/调试期） |
| L1 | 5.0 | 1.5 | 1.5 | 中等 |
| L2 | 2.0 | 1.2 | 1.2 | 严格（默认，`DEFAULT_PRECISION_LEVEL = "L2"`） |

判定的对象不是误差本身，而是**误差比**：

\[ \mathrm{ratio}_k = \frac{\mathrm{metric}_k(\text{npu},\ \text{golden})}{\mathrm{metric}_k(\text{gpu},\ \text{golden}) + 10^{-7}}, \quad k \in \{\text{MARE}, \text{MERE}, \text{RMSE}\} \]

含义是：被测实现的每一项误差，最多只能是标杆实现误差的 L 档倍数。这样做的妙处：

- 阈值不再依赖算子的数值量纲（输出是 1e-3 还是 1e3 都适用）；
- 标杆自动吸收「该 dtype 的固有量化噪声」——标杆自己也 bf16，它的误差就是合理水平的标尺；
- 换 dtype、换 shape、换数据分布，档位不用重标定。

#### 4.3.2 核心流程

`normal_domain_pass`（正常域检查）的流程：

```text
输入 npu_normal, gpu_normal, golden_normal（都只含 |golden| ≥ threshold 的元素）
  ├─ calc_error_metrics(npu_normal, golden_normal) → npu_metrics
  ├─ calc_error_metrics(gpu_normal, golden_normal) → gpu_metrics
  ├─ ratio[k] = npu_metrics[k] / (gpu_metrics[k] + 1e-7)   # 分母防零
  ├─ 取 precision_levels_config[level] 的三档上限
  └─ 任一 ratio[k] > limit → 打印 FAIL 原因并 return False；否则 PASS
```

#### 4.3.3 源码精读

档位表（[test_ai_infra_aggregate_hidden.py:29-36](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/tests/st/test_ai_infra_aggregate_hidden.py#L29-L36)）：`DEFAULT_PRECISION_LEVEL = "L2"` 与 `precision_levels_config` 三行字典，L2 最严（数值最小），与 u7-l4 见过的 pypto 三方对比判定同源。

`normal_domain_pass`（[test_ai_infra_aggregate_hidden.py:132-158](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/tests/st/test_ai_infra_aggregate_hidden.py#L132-L158)）：第 145 行一行字典推导算出三个比值（分母 `+1e-7` 防标杆误差为零）；第 152-155 行遍历档位表，任何一项超限立即打印 `[NormalDomain] FAIL on {k}: {ratio} > {limit}` 并返回 False——失败信息里带指标名与数值，方便定位是「个别坏点（MARE）」还是「整体漂移（MERE/RMSE）」。

总入口 `precision_check`（[test_ai_infra_aggregate_hidden.py:190-222](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/tests/st/test_ai_infra_aggregate_hidden.py#L190-L222)）：先用 `split_normal_and_small_domain` 把 npu/gpu 两路各切成正常域与小值域，然后第 209-218 行分别检查——注意两个 `if … numel() > 0` 的空域保护：某域没有元素时跳过检查并视为通过（例如权重均值 1.0 的造数使小值域可能为空）。第 220-222 行要求两域同时通过。

#### 4.3.4 代码实践

**实践目标**：在 CPU 上用可控的合成数据验证比值判定与 L2 边界行为（无需 NPU）。

**操作步骤**（示例代码）：

```python
# 示例代码：ratio_demo.py（CPU 即可运行，需先复制 4.2 节的指标函数与 normal_domain_pass）
import torch
golden = torch.ones(100, dtype=torch.float64)
gpu    = golden * 1.010   # 标杆：每个点偏 1%
npu    = golden * 1.020   # 被测：每个点偏 2%  → 三个 ratio 都 = 2.0
print(normal_domain_pass(npu, gpu, golden, "L2"))   # 边界：2.0 不大于 2.0 → True
npu = golden * 1.021      # 被测偏 2.1% → ratio = 2.1
print(normal_domain_pass(npu, gpu, golden, "L2"))   # False（2.1 > 2.0）
print(normal_domain_pass(npu, gpu, golden, "L1"))   # True（2.1 ≤ 5.0）
```

**需要观察的现象**：ratio 恒等于两路相对偏差之比（此处 eps 在分子分母同现、恰好相消）；同一组数据在 L2 失败但 L1 通过。

**预期结果**：三行输出依次为 `True / False / True`（以 `[NormalDomain]` 打印中的 ratio≈2.0 / 2.1 佐证）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 MARE 的档位（L2 为 2.0）与 MERE/RMSE 相同，而 L0 里 MARE 却放到 10.0？

**答案**：MARE 只看最坏单点，天然方差大、易受个别离群点影响，宽松档（L0，调试期）给它更大的余地（10.0）；MERE/RMSE 是统计量、更稳定，所以三档都维持在小倍数。

**练习 2**：如果标杆实现的某项指标恰好为 0（完全精确），比值判定会怎样？

**答案**：分母 `gpu_metrics[k] + 1e-7` 的防零项生效，比值变得极大，几乎必然 FAIL。这是设计上有意的行为：标杆零误差意味着「该指标上没有可接受的误差预算」，被测任何偏差都该被看见。

**练习 3**：`precision_check` 为什么在两个域上都用 `numel() > 0` 判空？

**答案**：真实数据可能整域缺失（如本用例的造数几乎不产生小值域元素）。判空跳过避免对空张量做 max/mean（空张量 max 会直接报错），同时把「无样本」语义处理为「不设限」而非「失败」。

### 4.4 小值域特殊容差与 conftest/pytest.ini 的收集过滤

#### 4.4.1 概念说明

**小值域的动机**：相对误差在 \( |g| \to 0 \) 时爆炸。设 golden \( g = 2^{-20} \)、绝对误差 \( 2^{-17} \)，绝对量微不足道，相对误差却是 \( 2^{3} = 8 \)（800%）。用相对指标评判这些点必然误杀。因此本框架把元素按 \( |golden| \) 与 threshold 的关系切成两域：

- **正常域**：\( |g| \ge \mathrm{threshold} \)，用 4.3 节的比值判定；
- **小值域**：\( |g| < \mathrm{threshold} \)，改问一个更宽松的问题——「绝对误差超过 `error` 门限的**坏点个数**，是否不超过标杆的 2 倍」。

各 dtype 的配置（[test_ai_infra_aggregate_hidden.py:40-44](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/tests/st/test_ai_infra_aggregate_hidden.py#L40-L44)）：

| dtype | threshold | error | 说明 |
| --- | --- | --- | --- |
| float16 | \(2^{-11}\) | \(2^{-16}\) | 11 位有效尾数 |
| bfloat16 | \(2^{-8}\) | \(2^{-16}\) | 8 位有效尾数 |
| float32 | \(2^{-14}\) | \(2^{-30}\) | 更精细的格式给更小的门限 |

对 bfloat16 的两个数（本讲实践任务要求的解释）：

- **threshold \(= 2^{-8} \approx 3.9\times10^{-3}\)**：bf16 只有 8 位有效尾数（1 隐含 + 7 显式），相对分辨率约为 \( 2^{-8} \)。golden 绝对值小于它的元素，「相对误差」这个概念已经开始失真——你量的是格点间距的噪声，不是实现的优劣。所以以 \( 2^{-8} \) 为界把它们划出正常域。
- **error \(= 2^{-16} \approx 1.5\times10^{-5}\)**：小值域内判定「坏点」的绝对误差门限，量级上恰为 threshold 的平方（\((2^{-8})^2 = 2^{-16}\)），可理解为「小值 × 一个小分辨率」的二次量级噪声容忍——偏离小于它的点不计较，超过才算坏点。

**收集过滤的动机**：st 用例必须真机，CI 或本机可能只有部分卡型。仓库用 `resources` marker 让每个用例声明需求（如 `device="npu:*", npus_per_node=1`），再由公共 conftest 在收集阶段把不匹配的用例 deselect，避免「没有 NPU 就满屏 ERROR」。

#### 4.4.2 核心流程

小值域判定流程（`small_domain_pass`）：

```text
输入 npu_small, gpu_small, golden_small（都只含 |golden| < threshold 的元素）
  ├─ error_count(actual) = Σ [ |golden| < threshold 且 |actual - golden| > error ]
  ├─ ratio = npu_cnt / max(gpu_cnt, 1)      # 分母取 max(·,1)：标杆零坏点时按 1 计
  └─ ratio ≤ 2.0 → PASS
```

收集过滤流程（`conftest.py`）：

```text
pytest 收集所有用例
  ├─ 无 resources marker → deselected（st 公共目录下未声明的用例一律不跑）
  ├─ device 不匹配（fnmatch 双向通配） → deselected
  ├─ --nodes 与需求不符 → deselected
  ├─ --npus-per-node 与需求不符 → deselected
  └─ 其余 selected
```

#### 4.4.3 源码精读

切域函数（[test_ai_infra_aggregate_hidden.py:107-128](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/tests/st/test_ai_infra_aggregate_hidden.py#L107-L128)）：按 `abs_golden >= threshold` 与 `< threshold` 两个布尔掩码切片，返回四元组（normal_actual/normal_golden/small_actual/small_golden）。

小值域检查（[test_ai_infra_aggregate_hidden.py:162-186](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/tests/st/test_ai_infra_aggregate_hidden.py#L162-L186)）：内嵌 `error_count` 用「且」连接两个条件统计坏点；第 179 行 `ratio = npu_cnt / max(gpu_cnt, 1)` 防标杆零坏点除零；第 186 行 `ratio <= 2.0` 硬编码通过线。一个读码细节：传入的 `golden` 已是小值域切片，`|golden| < threshold` 条件其实恒真，属于「防御性重复」——保留它让函数对未切片的整张 golden 也正确。

conftest 的选项注册（[conftest.py:18-48](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/st/conftest.py#L18-L48)）：`pytest_addoption` 往 `resources` 选项组挂 `--device`、`--nodes`、`--npus-per-node` 三个命令行参数（try/except 吞掉「参数已存在」的重复注册）；`pytest_configure` 注册 `resources` marker 与一条 filterwarnings。

设备匹配（[conftest.py:54-76](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/st/conftest.py#L54-L76)）：`device_match` 支持双向 fnmatch——marker 声明 `npu:*`、CLI 给 `npu:910B`，或反过来，都能匹配；CLI 未给 `--device` 时视为通配。

核心筛选（[conftest.py:83-123](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/st/conftest.py#L83-L123)）：`pytest_collection_modifyitems` 遍历收集项，**没有 `resources` marker 的用例直接 deselected**（第 95-98 行），然后依次做 device / nodes / npus_per_node 三个匹配，最后用 `pytest_deselected` 钩子把筛掉的用例标记为 deselected 而非 error。marker 的声明在 [pytest.ini:9-11](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/st/pytest.ini#L9-L11)。

**一个需要提醒的工程事实**：这对 conftest/pytest.ini 位于 `ascendc/src/tests/st/`，而各算子的 st 用例在 `ascendc/src/ops-transformer/<家族>/<算子>/tests/st/` 下，**并不在该目录树内**。pytest 只加载「参数路径公共祖先向下」的 conftest，因此要让 marker 注册与筛选生效，应以 `ascendc/src/tests/st` 为 rootdir/工作目录去 pytest 各算子用例路径（或自行复制这套 conftest/pytest.ini 到运行目录）。具体调用命令与排错在 u8-l4 展开，此处标注：**待本地验证**。

#### 4.4.4 代码实践

**实践目标**：用合成数据观察「相对误差在小值处爆炸、坏点计数却冷静」的对比（CPU 即可）。

**操作步骤**（示例代码）：

```python
# 示例代码：small_domain_demo.py（CPU 即可运行，需复制 split/指标/small_domain_pass 函数）
import torch
golden = torch.tensor([2**-9, 2**-10, 2**-20, 1.0], dtype=torch.float64)
actual = golden + torch.tensor([2**-17, 0.0, 2**-17, 1e-4], dtype=torch.float64)
# bf16 配置：threshold=2^-8, error=2^-16
n, gn, s, gs = split_normal_and_small_domain(actual, golden, torch.bfloat16)
print(n.numel(), s.numel())                      # 1 个正常域点，3 个小值域点
print(relative_error(actual[2:3], golden[2:3]))  # 2^-17/2^-20 = 8 → 相对误差 800%
print(small_domain_pass(actual, actual*0, golden, torch.bfloat16))  # 零标杆的行为
```

**需要观察的现象**：\( g=2^{-20} \)、偏差 \( 2^{-17} \) 的点相对误差为 8（800%），但若 error 门限是 \( 2^{-16} \)，该点偏差 \( 2^{-17} < 2^{-16} \) 根本不计为坏点。

**预期结果**：正常域 1 个元素、小值域 3 个元素；相对误差打印 8.0 附近。最后一句零标杆调用会得到一个极大的 ratio（`max(gpu_cnt,1)` 分母为 1）——体会坏点计数语义与相对误差语义的分野。

**实践目标 2**：`pytest --collect-only` 观察筛选。在有 pytest 的环境下，以 `ascendc/src/tests/st` 为工作目录对 aggregate_hidden 的 st 文件做 `--collect-only`，再对比加/不加 `--device npu:910B` 的收集差异（命令细节待本地验证，见 u8-l4）。

#### 4.4.5 小练习与答案

**练习 1**：bfloat16 的 `threshold=2^-8` 这个数和 bf16 的格式有什么内在联系？

**答案**：bf16 有 8 位有效尾数（1 隐含 + 7 显式），其相对分辨率约为 \( 2^{-8} \)。golden 绝对值低于此界时，格式本身的量化格距与被测值同量级，相对误差失去判别力，故以此为小值域边界。

**练习 2**：小值域判定为什么改用「坏点个数比」而不是继续用 MARE/MERE 比值？

**答案**：小值域内相对误差被极小分母放大，MARE/MERE 会系统性失真；坏点计数只问「绝对偏差是否超过一个极小门限」，对小值元素是语义正确的宽松判据，且以标杆坏点数为分母仍保留比值框架的自适应性。

**练习 3**：一个 st 用例若忘写 `@pytest.mark.resources(...)`，在启用了这套 conftest 的运行中会发生什么？

**答案**：它会在收集阶段被 `pytest_collection_modifyitems` 直接 deselected（[conftest.py:95-98](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/st/conftest.py#L95-L98)），不报错、不执行——表现为「用例被静默跳过」，排查时需检查 marker。

### 4.5 对照：op_api UT 为什么替代不了 ST

#### 4.5.1 概念说明

u8-l1 讲过 op_api UT 只测 aclnn 两段式接口的第一段 `GetWorkspaceSize`。它的替身哲学（TensorDesc 描述张量、宏组装参数、只断言返回码）决定了它**看不见数值**：executor 里规划的是「任务流水线」，没有一个比特真的被计算。ST 是唯一让「真实数据 × 真实 kernel × 真实调度」走完整链路并检查精度的层级。

#### 4.5.2 核心流程

一个 op_api UT 用例的形状（对照 FA 的用例）：

```text
TensorDesc({shape}, ACL_BF16, ACL_FORMAT_ND).ValueRange(-1,1)   ← 声明式张量（无真实数据）
        │
OP_API_UT(aclnnXxx, INPUT(…15 项…), OUTPUT(…4 项…))            ← 组装两段式第一段调用
        │
ut.TestGetWorkspaceSizeWithNNopbaseInner(&workspaceSize, &executor)
        │
EXPECT_EQ(返回值, ACLNN_SUCCESS)                                  ← 只断言流程成功
```

#### 4.5.3 源码精读

以 FlashAttention 前向的 UT 为例（[test_aclnn_flash_attention_score_enhance.cpp:39-112](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/tests/ut/op_api/test_aclnn_flash_attention_score_enhance.cpp#L39-L112)）：第 43-61 行用 `TensorDesc({256, 2, 192}, ACL_BF16, ACL_FORMAT_ND).ValueRange(-1,1)` 这类声明描述输入输出（可选输入直接给 `nullptr`，对应 u4-l1 读过的 18 输入清单）；第 63-73 行是标量属性（`scaleValue`、`headNum`、`inputLayout="TND"` 等）；第 75-104 行 `OP_API_UT(...)` 宏把 15 个输入、4 个输出打包成对 `aclnnFlashAttentionVarLenScoreEnhanceV5` 第一段的调用；第 105-108 行拿 `workspaceSize` 与 `executor`，`EXPECT_EQ(getWorkspaceResult, ACLNN_SUCCESS)` 收尾。整个文件 1107 行全是这种「参数组合 × 返回码」的用例（如第 117-120 行的空指针校验用例），没有一处数值断言。gtest fixture 基类见 [test_aclnn_flash_attention_score_enhance.cpp:24-36](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/tests/ut/op_api/test_aclnn_flash_attention_score_enhance.cpp#L24-L36)。

两套体系的对照表：

| 维度 | op_api UT（本文件） | ST（4.1-4.4 节） |
| --- | --- | --- |
| 运行环境 | 宿主机，无 NPU（stub/桩替身，见 u3-l4） | 真机 NPU |
| 输入 | TensorDesc 声明，无数据 | 固定种子的真实张量 |
| 覆盖范围 | GetWorkspaceSize 一段的校验/规划逻辑 | 全链路：tiling→kernel→精度 |
| 断言对象 | 返回码 `ACLNN_SUCCESS` | MARE/MERE/RMSE 比值 + 坏点数比 |
| 失败定位 | 参数校验分支 | 数值实现（kernel 算错/排布错/精度损失） |

#### 4.5.4 代码实践

**实践目标**：为「同一个算子家族，UT 与 ST 各抓什么 bug」建立映射。

**操作步骤**：

1. 在 [test_aclnn_flash_attention_score_enhance.cpp](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/tests/ut/op_api/test_aclnn_flash_attention_score_enhance.cpp#L114-L120) 中找一个「空指针/非法参数」用例（第 114 行起），写下它断言的返回码。
2. 在 [test_ai_infra_aggregate_hidden.py:299-300](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/tests/st/test_ai_infra_aggregate_hidden.py#L299-L300) 找到数值断言。
3. 填写对照表：左列「假设的 bug」（如「tiling 漏检空指针」「kernel 里 y0/y1/y2 状态递推写反」），右列勾选它能被 UT 还是 ST 抓住。

**需要观察的现象**：参数校验类 bug 与返回码强相关（UT 抓）；计算逻辑/排布类 bug 返回码仍是 SUCCESS，只有数值会说话（ST 抓）。

**预期结果**：完成一张 4~6 行的「bug → 抓手」对照表。（纯阅读型实践，无需运行。）

#### 4.5.5 小练习与答案

**练习 1**：`TensorDesc({256, 2, 192}, ACL_BF16, ACL_FORMAT_ND).ValueRange(-1,1)` 里的 `ValueRange` 在 UT 中起什么作用？

**答案**：它只是给描述对象标注一个「假想取值范围」元信息，供第一段接口内部逻辑（如按值分支的校验）参考；UT 并不会真的生成这些数据参与计算——这正是 UT 与 ST 的本质差别。

**练习 2**：如果一个 kernel 把输出排布写错（数值错、但不越界、不崩溃），UT 与 ST 各是什么表现？

**答案**：UT（含 op_api UT 与 tiling UT）全部通过——它们不计算数值；ST 的 `precision_check` 会因 MARE/MERE/RMSE 比值超标或坏点过多而 FAIL。这就是 st 不可裁剪的原因。

## 5. 综合实践

**任务**：为 aggregate_hidden 的 st 用例做三件事——(1) 补一个「CPU golden vs NPU 输出」的对比函数；(2) 新增 `S=8K, B=2, H=192*8` 的参数化用例；(3) 写清楚 bfloat16 小值域两个常数的含义。前两件需要 NPU 真机才能跑通完整链路，无真机时可先完成代码并做 CPU 侧自测（下述第 4 步），标注「待本地验证」。

**第 1 步：对比函数。** 框架的比值判定需要「标杆」做分母；没有第二路真机实现时，可用 **CPU 上同 dtype（bf16）直接计算**的结果当标杆，语义仍是「被测误差 / 同精度合理误差」。加入（示例代码，追加到 `test_ai_infra_aggregate_hidden.py`）：

```python
# 示例代码：CPU 标杆版精度判定（复用文件内已有的 split/normal/small 函数）
def precision_check_npu_vs_cpu(
    npu_out: torch.Tensor,      # 被测算子输出（NPU, bf16）
    cpu_ref_out: torch.Tensor,  # CPU 同 dtype 参考输出（bf16）
    golden: torch.Tensor,       # CPU float64 golden
    dtype: torch.dtype,
    precision_level: str = DEFAULT_PRECISION_LEVEL,
) -> bool:
    cpu_ref_out = cpu_ref_out.to(npu_out.device)      # 与 golden 同设备后再切片
    (npu_normal, golden_normal, npu_small, golden_small) = split_normal_and_small_domain(npu_out, golden, dtype)
    (cpu_normal, _, cpu_small, _) = split_normal_and_small_domain(cpu_ref_out, golden, dtype)
    normal_ok = (normal_domain_pass(npu_normal, cpu_normal, golden_normal, precision_level)
                 if golden_normal.numel() > 0 else True)
    small_ok = (small_domain_pass(npu_small, cpu_small, golden_small, dtype)
                if golden_small.numel() > 0 else True)
    print(f"[Final] normal_pass={normal_ok}, small_pass={small_ok}")
    return normal_ok and small_ok
```

**第 2 步：保留 bf16 的 CPU 参考输出。** 原用例在第 287-288 行把 `x_cpu`/`merge_conv_cpu` 原地升精度，bf16 结果没有留底。需在升精度**之前**补一行：

```python
# 示例代码：插在「cpu 升精度」之前
conv_out_cpu_bf16 = aggregate_hidden_torch(x_cpu, merge_conv_cpu, mask)  # bf16 CPU 标杆
```

**第 3 步：新用例。** `H = 192*8 = 1536`（是 192 的倍数、落在 [384, 24576] 内）、`S = 8192 ≤ 32K`、`B = 2 ≤ 8`，全部满足 u2-l1 读到的算子约束；张量约 `8192*2*1536*2B ≈ 48MB`，单卡可承受：

```python
# 示例代码：新增参数化用例（结构与 test_aggregate_hidden_network_shape1 相同）
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
@pytest.mark.resources(device="npu:*", npus_per_node=1)
@pytest.mark.parametrize("B,S,H", [(2, 8192, 192 * 8)])
def test_aggregate_hidden_network_shape2(self, B, S, H):
    sliding_window = 3
    dType = torch.bfloat16
    np.random.seed(54)
    x_cpu = torch.tensor(np.random.uniform(0, 1, (S, B, H)), requires_grad=True, dtype=dType)
    merge_conv_cpu = torch.nn.Conv1d(H, H, sliding_window, groups=H, bias=False).to(dtype=dType)
    torch.nn.init.normal_(merge_conv_cpu.weight, mean=1.0, std=0.01)
    # ...（三路分身与 shape1 完全一致，略）...
    result = precision_check_npu_vs_cpu(conv_out_npu, conv_out_cpu_bf16, conv_out_cpu, dType)
    assert result is True, "not pass precision check"
```

注意：pytest 参数化方法放在 `TestCase`（unittest 风格）类上时，参数需通过类级 `pytest.mark.parametrize` 或改用普通 pytest 类；最稳妥的做法是镜像 shape1 写一个独立方法（不含 parametrize）。**待本地验证。**

**第 4 步（无真机的自测）**：用 4.3/4.4 节的合成数据脚本在 CPU 上验证 `precision_check_npu_vs_cpu` 的分支（golden 全正常域 / 全小值域 / 混合），确认判空与比值逻辑正确。

**第 5 步：解释小值域常数（书面作业，答案已含于 4.4.1）**：bfloat16 的 `threshold=2^-8` 对应其 8 位有效尾数的相对分辨率，是「相对误差开始失真」的边界；`error=2^-16` 是小值域内不计较的绝对噪声门限，量级为 threshold 的平方（\((2^{-8})^2=2^{-16}\)）。

**验收标准**：函数与新用例代码合入测试文件后语法检查通过（`python -m py_compile`）；有真机时 `pytest` 可执行新用例并通过（运行链路见 u8-l4）；无真机时完成第 4、5 步并明确标注待验证项。

## 6. 本讲小结

- ST 与 UT 分工明确：UT（faker/stub，无真机）验证流程与分支，ST（真机 + 真数据）验证数值精度；op_api UT 只断言 `GetWorkspaceSize` 返回码，看不见一个比特的计算结果。
- 仓库的 st 精度框架是**三方对比**：CPU float64 golden（真值）+ NPU 同 dtype 标杆（合理水平）+ 被测算子输出，判定对象是「误差比」而非绝对误差，因此阈值与数值量纲、dtype、shape 解耦。
- 三把尺子缺一不可：MARE 抓个别坏点、MERE 抓整体相对漂移、RMSE 抓绝对幅度偏差；`calc_error_metrics` 以 `golden.dtype == float64` 为硬契约统一出口。
- L0/L1/L2 是三档比值许可（L2 默认最严：MARE≤2.0、MERE/RMSE≤1.2），失败打印具体超限指标，便于定位坏点型还是漂移型问题。
- 小值域（\(|golden| <\) threshold）放弃相对指标，改用「绝对误差超过 error 的坏点个数 ≤ 标杆 2 倍」；bfloat16 的 `2^-8 / 2^-16` 分别对应 8 位有效尾数的分辨率边界与二次量级的噪声门限。
- st 公共设施在 `ascendc/src/tests/st/`：`conftest.py` 用 `resources` marker + `--device/--nodes/--npus-per-node` 做收集期筛选（无 marker 一律 deselect），`pytest.ini` 注册 marker；算子用例分布各算子目录，运行时需注意 rootdir 挂接（u8-l4 展开）。

## 7. 下一步学习建议

本讲完成了一个 st 用例的「读」与「写」。下一讲 **u8-l4（构建与运行 UT/ST）** 把链路跑起来：`build.sh -u` 的 UT 构建分支、wheel 安装后用 pytest 在 st 目录筛选执行（`-k aggregate_hidden`），以及 `requirements.txt` 缺依赖、rootdir/conftest 挂接等排错点。进阶阅读建议：

1. `ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/tests/st/test_sinkhorn_grad.py`——反向算子的 st 怎么组织 golden 与梯度对比；
2. `ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/tests/st/lightning_indexer_klloss_golden.py`——独立 golden 文件的拆分写法；
3. 对照 u7-l4 的 pypto st 体系，体会「同一套 MARE/MERE/RMSE + L0/L1/L2 思想」在 Python 算子库中的变体（`precision_compare_triple` 三方判分）。
