# 精度验证与 pytest 测试实践

## 1. 本讲目标

学完本讲，你应该能够：

1. 读懂 ops-transformer 中一个 pytest 精度测试工程的组织方式（用例集、CPU 标杆、NPU 直调、比对入口）。
2. 理解两类精度验证路径的分工：kernel UT 的 `gen_data.py`/`compare_data.py` 数据脚本闭环，与 pytest 级的 CPU/NPU 双实现对拍。
3. 掌握 rtol/atol 容差判定的数学含义，理解 fp16/bf16/fp32 不同数据类型下精度验收标准的差异。
4. 能为一个真实算子（flash_attention_score）补充自定义 shape 的 pytest 用例并运行。

## 2. 前置知识

- **golden 数据（标杆数据）**：算子验证的基本范式是「用一份被认为正确的参考实现算出期望结果，再和被测实现比对」。参考实现通常跑在 CPU 上、用高精度类型计算，这份期望结果就叫 golden。
- **rtol 与 atol**：浮点数逐元素比较很少要求「完全相等」，而是要求偏差落在容差内。numpy 的 `np.isclose(a, b, rtol, atol)` 判定条件是：

  \[ |a - b| \le atol + rtol \times |b| \]

  - `rtol`（相对容差）允许结果随数值大小等比例偏移，适合数值范围大的场景；
  - `atol`（绝对容差）给接近 0 的数值兜底——因为 \( 0.001 \) 相对于 \( 0.0001 \) 是 10 倍误差，但绝对量微不足道。
- **为什么不同 dtype 验收标准不同**：fp16 有 10 位尾数、bf16 只有 7 位尾数、fp32 有 23 位。位数越少，单次乘加的舍入误差越大，几十次累加后误差按算子内部计算路径不同而放大。所以：
  - 逐元素简单算子（如 add）可以做**逐位精确比对**；
  - 含大量乘加与 softmax 指数运算的算子（如 attention）只能做**统计容差比对**——允许小比例元素超差，统计整体偏差。
- **ut 与 st**：本仓库的测试语境里，**ut**（单元测试）指 `tests/ut/` 下的 C++ gtest 用例（host 侧 tiling/infershape、kernel 侧 CPU 仿真执行），由 `build.sh --ophost_test/--opkernel_test` 驱动；**st**（系统测试）语境更宽，本讲涉及的 pytest 精度测试属于「真机上的功能/精度验证」——它不 mock 任何层，直接通过 TorchNPU 调用装好的算子，验证的是端到端精度。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [attention/flash_attention_score/tests/pytest/README.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/tests/pytest/README.md) | FA 算子 pytest 测试框架的说明：文件结构与运行方式 |
| [attention/flash_attention_score/tests/pytest/test_case.py](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/tests/pytest/test_case.py) | 测试用例集：一个字典定义全部场景（shape/dtype/layout/sparse 等） |
| [attention/flash_attention_score/tests/pytest/test_flash_attn.py](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/tests/pytest/test_flash_attn.py) | 执行主程序：pytest 入口、CPU/NPU 双跑、精度比对 `check_result` |
| [attention/flash_attention_score/tests/pytest/cpu_impl.py](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/tests/pytest/cpu_impl.py) | CPU 参考实现：用 fp32 复现 FA 前向，生成 golden |
| [attention/flash_attention_score/tests/pytest/npu_impl.py](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/tests/pytest/npu_impl.py) | NPU 实现：通过 `torch_npu.npu_fusion_attention_v2` 真机直调算子 |
| [attention/flash_attention_score/tests/pytest/test_utils.py](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/tests/pytest/test_utils.py) | 工具方法：QKV/pse/mask 数据生成、layout 转换、dropout mask 复现等 |
| [examples/add_example/tests/ut/op_kernel/add_example_data/gen_data.py](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/tests/ut/op_kernel/add_example_data/gen_data.py) | kernel UT 的输入与 golden 生成脚本（numpy 实现） |
| [examples/add_example/tests/ut/op_kernel/add_example_data/compare_data.py](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/tests/ut/op_kernel/add_example_data/compare_data.py) | kernel UT 的输出与 golden 比对脚本 |
| [examples/add_example/tests/ut/op_kernel/test_add_example.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/tests/ut/op_kernel/test_add_example.cpp) | kernel UT 的 C++ 驱动：`ICPU_RUN_KF` CPU 仿真执行 kernel 并串起两个脚本 |
| [tests/test_config.yaml](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/tests/test_config.yaml) | CI 看护配置：哪些算子触发 example/ut 验证 |

## 4. 核心概念与源码讲解

### 4.1 pytest 精度测试框架：五件套分工

#### 4.1.1 概念说明

大算子的精度验证要回答一个问题：「NPU 上跑出来的结果，和数学上正确的结果差多少？」ops-transformer 对此采用了**双实现对拍**（也叫对背靠背）的结构：

- 用纯 PyTorch 在 CPU 上写一份**高精度参考实现**（golden 来源）；
- 用 TorchNPU 在真机上**直调算子**拿到实际输出；
- 两者逐元素比对，输出精度指标。

这五份文件各管一段：`test_case.py` 管「测什么」，`cpu_impl.py` 管「什么是对的」，`npu_impl.py` 管「怎么调真机」，`test_flash_attn.py` 管「怎么比」，`test_utils.py` 提供公共数据构造。

#### 4.1.2 核心流程

```text
pytest -s
  └─ test_npu_flash_attn()              # pytest 自动发现的入口
      └─ 遍历 test_case.py 的 TestCases 字典
          ├─ generate_qkv / generate_pse / generate_npu_mask   # 构造输入
          ├─ tforward(...)               # cpu_impl: fp32 参考实现 → golden
          ├─ fa_npu(...)                 # npu_impl: npu_fusion_attention_v2 真机直调
          └─ check_result(golden, npu)   # 逐元素容差比对，打印精度指标
```

#### 4.1.3 源码精读

框架说明直接写在 README 里——CPU 侧生成 golden、NPU 侧 TorchNPU 直调、双侧精度对比，运行方式是在 pytest 目录下 `pytest -s`：

- [attention/flash_attention_score/tests/pytest/README.md:L1-L37](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/tests/pytest/README.md#L1-L37) —— 定义了五件套的文件结构与「CPU 复现算子功能生成 golden、NPU 通过 TorchNPU 直调取实际数据、双侧对比」的方法论。

用例集是一个纯数据字典，每个条目一个场景，键名自带语义（GQA/MLA/ALIBI/SPARSE）。注意条目里只写「与默认不同的部分」：如 `MLA_02` 没写 `B` 和 `Sq`（TND 变长格式下由 `actual_seq_qlen` 推导），缺省参数在执行主程序里用 `kwargs.get(..., 默认值)` 补齐：

- [attention/flash_attention_score/tests/pytest/test_case.py:L46-L67](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/tests/pytest/test_case.py#L46-L67) —— `TestCases` 字典的前两个条目：`GQA_01` 覆盖 GQA（N1=16 个 query 头对 N2=8 个 kv 头）+ causal；`MLA_02` 覆盖 TND 变长 + rope（DRope=64）。
- [attention/flash_attention_score/tests/pytest/test_case.py:L14-L43](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/tests/pytest/test_case.py#L14-L43) —— 文件头部注释逐个解释 B/N1/N2/Sq/Skv/D/DV、sparse_mode、pse_type 等参数含义，是理解用例字段的权威说明。

执行主程序把「取参数 → 生成输入 → 双侧执行 → 比对」串成一条链：

- [attention/flash_attention_score/tests/pytest/test_flash_attn.py:L61-L110](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/tests/pytest/test_flash_attn.py#L61-L110) —— `call_flash_attn`：用 `kwargs.get` 补齐缺省参数（如 `scale` 默认 \( 1/\sqrt{D} \)），生成 pse/qkv/mask，先跑 `tforward` 得 golden，再跑 `fa_npu` 得真机结果，最后对 out/max/sum 三个输出各调一次 `check_result`。
- [attention/flash_attention_score/tests/pytest/test_flash_attn.py:L113-L117](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/tests/pytest/test_flash_attn.py#L113-L117) —— `test_npu_flash_attn`：pytest 按 `test_` 前缀自动发现它，遍历 `TestCases` 逐条执行。**新增用例只需在字典里加条目，不用改这里的代码**。

CPU 参考实现的精髓是「先升精度再计算」：

- [attention/flash_attention_score/tests/pytest/cpu_impl.py:L40-L70](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/tests/pytest/cpu_impl.py#L40-L70) —— `forward`：入口先把 q/k/v 全部 `q.float()` 升到 fp32，再按 \( QK^T \to \text{scale/pse} \to \text{mask} \to \text{softmax} \to \times V \) 的数学定义逐步计算。softmax 用的是减最大值的数值稳定写法（`tsoftmax`，L20-L26），与 NPU 侧算法在数学上等价但实现路径完全独立——这是对拍有效的前提。

NPU 侧通过 PyTorch 的 TorchNPU 扩展调用算子：

- [attention/flash_attention_score/tests/pytest/npu_impl.py:L42-L82](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/tests/pytest/npu_impl.py#L42-L82) —— `fa_npu`：把 CPU 生成的 tensor 按 layout 转换后 `.to(device)` 搬到 NPU，调 `torch_npu.npu_fusion_attention_v2`（它内部封装了 u2-l4/u3-l1 讲过的 aclnn 两段式调用），`torch.npu.synchronize()` 等待异步任务完成，再把结果 `.cpu()` 搬回来比对。

#### 4.1.4 代码实践

1. **实践目标**：在不运行的情况下，人工走通一条用例的参数流转。
2. **操作步骤**：打开 `test_case.py` 中的 `SPARSE_06` 条目（B=4, N1=8, Sq=256, D=768, SBH, bf16, sparse_mode=0, pre/next_tokens=128），对照 `test_flash_attn.py` 的 `call_flash_attn` 逐个写出该条目各参数的实际取值（缺省的 `N2`、`Skv`、`DV`、`scale` 分别是多少）。
3. **需要观察的现象**：你会得到 N2=8（默认等于 N1）、Skv=256（默认等于 Sq）、DV=768（默认等于 D）、\( scale = 1/\sqrt{768} \)。
4. **预期结果**：理解「用例字典只写差异、执行器负责补默认值」的组织方式，这是本框架新增用例成本低的原因。

#### 4.1.5 小练习与答案

**练习 1**：为什么 golden 用 fp32 计算而不是直接用 fp16 复现算子路径？
**答案**：参考实现的职责是「数学正确」，不是「复现舍入行为」。fp32 计算的舍入误差远小于 fp16/bf16，可以近似看作真值；若用 fp16 复现，参考实现自己就带入了与被测实现相同量级的误差，比对就失去意义。

**练习 2**：`test_npu_flash_attn` 里循环执行所有用例，如果某条用例失败，后续用例还会执行吗？这有什么优缺点？
**答案**：会继续执行（循环内没有 early return，`check_result` 也只打印不抛异常）。优点是一次运行能拿到全部用例的精度画像；缺点是 pytest 不会因为精度超差而标记 FAIL，需要人工看输出中的 warning 行（u7-l1 的 gtest UT 才是 CI 门禁用的失败即退）。

### 4.2 精度判定算法：check_result 的容差模型

#### 4.2.1 概念说明

`check_result` 是本框架的「裁判」，它实现的判定模型可以概括为：

1. **完全一致短路**：若逐位相等，直接返回满分，跳过统计。
2. **逐元素容差**：对每个元素计算阈值 \( t_i = \max(rtol \times \max(|g_i|, |r_i|),\ floor) \)，其中 floor 是一个很小的绝对下限；偏差 \( |g_i - r_i| > t_i \) 的元素记为「超差元素」。
3. **统计验收**：超差元素占比 \( ratio \) 不超过 `threshold_diff` 即视为通过，同时打印 `diff_max`（最大绝对偏差）与 `diff_sum`（偏差总和）作为观测指标。

这比 `np.isclose` 的逐元素判定更宽容：允许极少数元素（如 softmax 边界、指数溢出附近的值）超差，只要整体分布健康。

#### 4.2.2 核心流程

```text
输入: expect(golden), result(npu), 两端已 .float()
├─ shape 不匹配 → 直接判错（打印 expect/result shape）
├─ torch.all(eq) → 完全一致, ratio_diff=0
└─ 否则:
     diff = |expect - result|
     threshold_i = max( max(|expect_i|,|result_i|) * 0.005 , 2.5e-5 )
     ratio_diff = count(diff_i > threshold_i) / numel
     └─ ratio_diff > 0.005 → 打印 warning（超差）; 否则打印 info
     打印 diff_max / diff_sum
```

即 rtol = 0.5%、atol 下限 \( 2.5 \times 10^{-5} \)、超差占比阈值 0.5%。

#### 4.2.3 源码精读

- [attention/flash_attention_score/tests/pytest/test_flash_attn.py:L23-L58](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/tests/pytest/test_flash_attn.py#L23-L58) —— `check_result` 全文：`ratio_threshold=0.005`、`threshold_diff=0.005` 两个常量定义在函数头部；L37-L39 构造逐元素阈值 `threshold = max(max(|expect|,|result|) * rtol, 2.5e-5)`，L40-L41 统计超差占比；注意 L47-L50 的 `ratio`/`max`/`sum` 三个指标即使通过也会打印——它们就是要记录的精度指标。

一个值得注意的细节：阈值取的是 golden 与 result 的**较大者**乘 rtol（`torch.max(torch.abs(expect), torch.abs(result))`），而不是 numpy `isclose` 公式里只用被比较方。这在两值符号相反或量级悬殊时更稳健。

#### 4.2.4 代码实践

1. **实践目标**：用 Python 亲手复现一次容差判定，建立数值直觉。
2. **操作步骤**（以下为示例代码，可存成独立小脚本在任意有 numpy/torch 的环境运行）：

   ```python
   # 示例代码：复现 check_result 的判定逻辑
   import torch
   golden = torch.tensor([1.0, 100.0, 0.00001, -3.0])
   result = torch.tensor([1.002, 99.0, 0.00003, -3.0])
   rtol, floor = 0.005, 2.5e-5
   diff = torch.abs(golden - result)
   threshold = torch.max(torch.max(golden.abs(), result.abs()) * rtol,
                         torch.full_like(golden, floor))
   over = diff > threshold
   print("各元素阈值:", threshold)
   print("超差掩码:", over, " 超差占比:", over.float().mean().item())
   ```

3. **需要观察的现象**：第 1 个元素偏差 0.002 < 阈值 0.005 通过；第 2 个元素偏差 1.0 但阈值 0.5 判超差（相对误差 1%）；第 3 个元素绝对偏差极小、由 floor 兜底通过。
4. **预期结果**：超差占比 0.25，未超 0.5% 的统计阈值——体会「相对容差 + 绝对下限 + 统计验收」三层设计的各自作用。

#### 4.2.5 小练习与答案

**练习 1**：bf16 用例（如 `GQA_01`）和 fp32 用例（`SPARSE_07` 是 torch.float32）共用同一套 0.5% 容差，合理吗？如果让你分 dtype 设阈值，会怎么调？
**答案**：偏宽松但可接受，因为 golden 一律 fp32 计算、误差主要来自被测侧的低位宽累加。若分 dtype，bf16（7 位尾数，相对舍入误差约 \( 2^{-7} \approx 0.8\% \)）可适当放宽 rtol，fp32 可收紧；更严谨的做法是按算子内部累加长度（Skv 越长误差累积越大）动态设置。

**练习 2**：`check_result` 为什么在 shape 不匹配时把 `max`、`sum` 置成 999999 而不是 0？
**答案**：这两个值是给外部观测/记录用的指标，999999 是明显的「哨兵值」，保证 shape 错误这种硬失败在指标上不可能被误读成精度良好（0 反而像完美通过）。

### 4.3 数据比对：kernel UT 的 gen_data / compare_data 闭环

#### 4.3.1 概念说明

pytest 对拍需要真机 + TorchNPU，属于「重量级」验证。对 kernel 本身，仓库还有一套「轻量级」闭环：**两个 numpy 脚本 + 一个 C++ UT 驱动**。

- `gen_data.py`：构造输入和 golden，写成 `.bin` 裸文件；
- C++ UT（gtest）读入输入，用 `ICPU_RUN_KF` 在 CPU 上仿真执行 kernel（u2-l3 讲过），把输出写成 `.bin`；
- `compare_data.py`：glob 拾取 `*golden*.bin` 与 `*output*.bin`，逐文件比对并打印 PASSED/FAILED。

这套闭环的关键特点是**不依赖 NPU**，可以在 x86 编译机上跑 kernel 逻辑验证，是 CI 的 UT 门禁主力。

#### 4.3.2 核心流程

```text
test_add_example.cpp (gtest, CPU 仿真)
  ├─ system("python3 gen_data.py '(32,4,4,4)' 'float32'")   # 生成 input/golden bin
  ├─ ReadFile(input.bin) → ICPU_RUN_KF(add_example<0>, ...)  # CPU 上跑 kernel
  ├─ WriteFile(output.bin)
  └─ system("python3 compare_data.py 'float32'")            # 比对, exit code 0/1
```

#### 4.3.3 源码精读

- [examples/add_example/tests/ut/op_kernel/add_example_data/gen_data.py:L25-L37](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/tests/ut/op_kernel/add_example_data/gen_data.py#L25-L37) —— `gen_data_and_golden`：解析命令行传入的 shape 字符串，用 `np.random.choice` 从一组精心挑选的边界值（±65504 是 fp16 最大值、还有 nan/inf/0/±0.5/±1）中采样——**边界值采样是故意的**，比均匀随机更容易暴露溢出与特殊值处理缺陷；golden 用 `np.add` 一行生成，最后 `tofile` 写裸 bin。
- [examples/add_example/tests/ut/op_kernel/add_example_data/compare_data.py:L22-L45](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/tests/ut/op_kernel/add_example_data/compare_data.py#L22-L45) —— `compare_data` 与 `get_file_lists`：按文件名通配符配对 golden 与 output；L30 的 `np.isclose(tmp_out, tmp_gold, 0, 0, True)` 四个实参分别是 rtol=0、atol=0、equal_nan=True——即**逐位精确比对**，只有 nan 视为相等。这对 add 这种逐元素恒等映射的算子足够；L36-L37 超差时打印前 5 个坏点索引与双侧取值，方便定位。
- [examples/add_example/tests/ut/op_kernel/add_example_data/compare_data.py:L55-L57](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/tests/ut/op_kernel/add_example_data/compare_data.py#L55-L57) —— 比对结果转进程退出码（`exit(0 if ret else 1)`），使 gtest 的 `system()` 调用能感知失败，CI 因此可以拿退出码做门禁。
- [examples/add_example/tests/ut/op_kernel/test_add_example.cpp:L61-L93](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/tests/ut/op_kernel/test_add_example.cpp#L61-L93) —— C++ 驱动中两处 `system("...gen_data.py ...")`、一处 `system("...compare_data.py ...")` 的调用点，以及中间的 `ICPU_RUN_KF(add_example<0>, numBlocks, x, y, z, workspace, tiling)` CPU 仿真执行——三步构成完整闭环。

#### 4.3.4 代码实践

1. **实践目标**：脱离 NPU，亲手体验「生成 → 比对」脚本闭环。
2. **操作步骤**（只需 numpy，任何 Python3 环境可做）：

   ```bash
   cd examples/add_example/tests/ut/op_kernel/add_example_data/
   python3 gen_data.py '(32, 4, 4, 4)' 'float16'   # 生成 float16_input/golden 两个 bin
   cp float16_input_add_example.bin float16_output_add_example.bin  # 假装输出
   python3 compare_data.py 'float16'
   # 再人为制造偏差验证负路径:
   python3 - <<'EOF'
   import numpy as np
   a = np.fromfile("float16_golden_add_example.bin", np.float16)
   a[3] += 1
   a.tofile("float16_output_add_example.bin")
   EOF
   python3 compare_data.py 'float16'
   ```

3. **需要观察的现象**：第一次打印 `PASSED!` 与 `compare result: True`；第二次打印 `FAILED!`、坏点 `index: 3` 的 output/golden 取值，且 `compare result: False`。
4. **预期结果**：两次运行分别返回退出码 0 和 1；理解 add 算子采用 rtol=0/atol=0 逐位比对的原因（逐元素加法不存在误差累积，fp16 加法 CPU 与 kernel 应得到完全一致的结果）。

#### 4.3.5 小练习与答案

**练习 1**：如果把 add_example 的 kernel 改成 `z = x + y` 后再做一次 fp16 乘法（如 u2-l3 的乘 2 改造），`compare_data.py` 的逐位比对还成立吗？
**答案**：大概率仍成立：乘 2 是精确操作（指数 +1，尾数不变），不会引入舍入。但若改成乘 3 这类尾数无法精确表示的系数，fp16 下 CPU 与 kernel 的舍入路径若不一致（如一侧先升精度再乘），逐位比对就可能失败——这正是 4.2 的统计容差存在的理由。

**练习 2**：`gen_data.py` 中 `np.random.choice` 的候选值列表为什么要包含 `np.nan` 和 `np.inf`？
**答案**：用于覆盖特殊值路径——检验 kernel 与参考实现对 nan/inf 的传播行为是否一致（如 `inf + (-inf) = nan`）。配合 `compare_data.py` 里 `equal_nan=True`，nan 位置对 nan 位置被视为相等，从而能对包含 nan 的数据正常比对。

### 4.4 ut 与 pytest 精度测试的关系：谁在什么时机跑

#### 4.4.1 概念说明

本仓库的验证体系是分层的：

| 维度 | kernel/host UT（gtest） | pytest 精度测试 |
| --- | --- | --- |
| 位置 | `<算子>/tests/ut/` | `<算子>/tests/pytest/` |
| 驱动 | `build.sh --ophost_test/--opkernel_test` | 目录下手动 `pytest -s` |
| 硬件 | 不需要 NPU（CPU 仿真） | 需要 NPU + TorchNPU + 已安装算子包 |
| 判定 | 逐位精确（rtol=0） | 统计容差（rtol=0.5% + 占比阈值） |
| 角色 | CI 门禁，改码必跑 | 开发/发布期的精度画像，人工判读 |

`tests/test_config.yaml` 是 CI 侧的「看护地图」：它声明每个算子目录被修改时要触发哪些验证，目前支持 `example` 与 `ut` 两类——pytest 精度测试不在 CI 自动触发范围内，主要靠人工按需执行。

#### 4.4.2 核心流程

```text
开发者修改某算子源码 → CI 解析 changed files
  → 命中 test_config.yaml 中某结点的 src
    → 触发该结点配置的验证（example / ut）
      → ut: build.sh --opkernel_test --ops=<op> 编译并运行 gtest
```

#### 4.4.3 源码精读

- [tests/test_config.yaml:L10-L43](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/tests/test_config.yaml#L10-L43) —— 配置文件的头部注释完整说明了结点树、`src`/`exclude`/`ut_cov_exclude`/`test`/`options` 各字段语义，其中明确「`test` 字段目前支持 example、ut」，以及 `options` 如何把一个算子的变更联动到需要一起验证的其他算子。

#### 4.4.4 代码实践

1. **实践目标**：在 `test_config.yaml` 中找到 flash_attention_score 结点，确认它被哪些验证看护。
2. **操作步骤**：打开 `tests/test_config.yaml`，搜索 `flash_attention_score`，记录其 `src`、`test`、`options` 三个字段的取值；再搜索 `add_example`（在 `examples:` 一级结点下）做同样记录。
3. **需要观察的现象**：两个结点各自配置了哪些触发条件、是否互相出现在对方的 `options` 里。
4. **预期结果**：能说出「改 FA 源码会触发哪些验证」；若某字段未配置，对照 L37-L43 的注释说明其默认行为（`test` 未填写的字段默认为 True）。具体取值以文件为准，此处不预设结论。

#### 4.4.5 小练习与答案

**练习 1**：为什么 CI 门禁选 gtest UT（逐位比对）而不是 pytest 精度测试（统计容差）？
**答案**：两个原因——① CI 的 x86 编译机没有 NPU，只能跑 CPU 仿真的 UT，pytest 必须真机；② 门禁要求失败判定客观明确（退出码），而 pytest 版 `check_result` 只打印指标不抛异常，需要人工判读精度画像，不适合做自动门禁。

**练习 2**：一套测试同时覆盖「CPU 仿真」和「真机」两条执行路径，分别验证了什么？
**答案**：CPU 仿真（`ICPU_RUN_KF`）验证 kernel 的计算逻辑正确性；真机（pytest + TorchNPU）额外覆盖了 tiling 真实策略、aclnn 两段式调用、驱动/运行时链路以及真机专用指令的数值行为——两条路径发现的 bug 类型不同，互补而非重复。

## 5. 综合实践

为 flash_attention_score 补充一个自定义 shape 的 pytest 用例并运行（本讲的核心实践）：

1. **准备**：按 u1-l3/u1-l4 完成环境部署，`source` CANN 环境变量，确认 `python3 -c "import torch, torch_npu"` 可用且 TorchNPU 为最新版本（pytest README 的前置要求），已安装含 flash_attention_score 的算子包（`--pkg` 产物或官方 ops 包）。
2. **添加用例**（示例代码，加在 `attention/flash_attention_score/tests/pytest/test_case.py` 的 `TestCases` 字典末尾）：

   ```python
   # 示例代码：短序列、小 batch 的自定义用例
   "MYCASE_SHORTSEQ": {
       "B": 1,
       "N1": 2,
       "N2": 2,
       "Sq": 64,
       "Skv": 64,
       "D": 64,
       "input_layout": "BSND",
       "dtype": torch.float16,
       "sparse_mode": 0,
   },
   ```

3. **运行**：

   ```bash
   cd attention/flash_attention_score/tests/pytest/
   pytest -s
   ```

4. **记录精度指标**：从输出中摘出 `MYCASE_SHORTSEQ` 用例 `out`/`max`/`sum` 三组比对的 `diff_max`、超差占比（info/warning 行）以及「计算结果完全一致」是否出现，整理成一张小表。
5. **判读**：若出现 warning 行（超差占比 > 0.5%）或 shape 不匹配的 error 行，回到 4.1/4.2 的源码定位是参数组合不受支持还是精度问题。
6. **无 NPU 环境时**：本实践无法执行，属「待本地验证」；可先完成 4.3.4 的纯 CPU 脚本闭环作为替代，并走读本实践步骤，为有真机时做准备。

## 6. 本讲小结

- ops-transformer 的算子精度验证有两条路径：**gtest UT 的 gen_data/compare_data 脚本闭环**（CPU 仿真、逐位比对、CI 门禁）与 **pytest 双实现对拍**（CPU fp32 golden vs 真机 TorchNPU 直调、统计容差、人工判读）。
- pytest 框架五件套分工：`test_case.py` 纯数据用例集（只写差异）、`test_flash_attn.py` 执行与比对、`cpu_impl.py` 高精度参考实现、`npu_impl.py` 真机直调、`test_utils.py` 数据构造工具；新增用例只需向字典加一个条目。
- `check_result` 的容差模型：阈值 \( \max(0.005 \times \max(|g|,|r|),\ 2.5\times10^{-5}) \)，超差占比 ≤ 0.5% 视为通过，同时打印 `diff_max`/`diff_sum` 作为精度画像。
- 精度标准与 dtype 和算子复杂度相关：add 这类逐元素算子可逐位比对（rtol=0），attention 这类含指数与长累加的算子必须统计容差；golden 一律 fp32 计算。
- `gen_data.py` 的边界值采样（±65504、nan、inf）与 `compare_data.py` 的 `equal_nan=True` 配合，用最小成本覆盖特殊值路径。
- `tests/test_config.yaml` 声明 CI 按变更文件触发 example/ut 验证；pytest 精度测试不进 CI，靠人工按需运行。

## 7. 下一步学习建议

- 下一讲 u4-l1 将进入 **Flash Attention 算子家族概览**：本讲你已在调用侧接触了 `npu_fusion_attention_v2` 与 GQA/MLA/ALIBI 等用例形态，下一讲从算法与版本演进角度系统讲解 FA 家族，建议先读 `attention/flash_attention_score/README.md`。
- 想深入测试工程化的读者，可继续阅读 `attention/flash_attention_score/tests/pytest/test_utils.py` 中 dropout mask 的 philox 随机数复现（L18-L100 附近）——它展示了如何让 CPU 参考实现逐位复现 NPU 侧随机数行为，是对拍工程里最难的一类问题。
- u7-l1（单元测试体系与 UT 框架）将系统讲解 ophost/opapi/opgraph/opkernel 四类 UT 的写法与 `test_config.yaml` 的裁剪作用，与本讲 4.4 节直接衔接。
