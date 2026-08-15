# u7-l2 Kernel UT 与 ST 验证：仿真执行与精度对账

## 1. 本讲目标

上一讲（u7-l1）我们掌握了 UT 体系的总框架，并学会了编写 Host 侧的 infershape UT 与 tiling UT。本讲继续深入 **Device 侧的 Kernel UT**，并横向认识 **ST 系统测试**，学完后你应当能够：

1. 说清 Kernel UT 为什么**不需要真实 NPU** 就能跑 AI Core kernel——理解 `tikicpulib` 仿真执行与 `ICPU_RUN_KF` 的原理。
2. 独立读懂并仿写一个 Kernel UT 用例：申请仿真 GM 内存、手工构造 TilingData、设置 tiling key、执行 kernel、校验结果。
3. 会用公共测试数据框架 `kernel_ut_data_helper` / `kernel_ut_data_executor` 组织「Python 生成数据 + C++ 执行 + Python 对账」的标准流程。
4. 理解 ST 测试（atk 配置 + executor + golden 函数 + fuzz 用例表）与 UT 的分工，能为一个新算子规划**最小测试集**。

## 2. 前置知识

- **仿真执行（CPU 模拟）**：Kernel UT 并不把 kernel 编成真二进制放到 NPU 上跑，而是把 Ascend C 源码编译成 **x86 主机上的可执行代码**，用 CANN 包提供的 `tikicpulib`（CPU 仿真库）在 Host 内存里模拟 GM/UB 与多核调度。这解释了两件事：为什么 UT 无需 NPU 环境，以及为什么 UT 里能直接 `#include` kernel 源文件。
- **gtest 回顾**：用例以 `TEST_F(套件名, 用例名)` 组织，`SetUpTestCase/TearDownTestCase` 做整个套件的准备与清理；断言 `EXPECT_EQ/EXPECT_NEAR` 失败仅记失败，`ASSERT_*` 失败则中止当前用例。
- **golden（金标准）对账**：精度验证的通用套路是「用一个可信的参考实现算出期望输出（golden），再与被测实现逐元素比对」。ops-nn 中参考实现通常用 PyTorch/NumPy 在 CPU 上完成，浮点比较需给出容差（如 `EXPECT_NEAR(a, b, 1e-6)`）。
- **承接前讲的关键概念**：TilingData 是 Host 写、Device 读的 POD 契约（u4-l2）；tiling key 决定 kernel 模板分支（u4-l1/u4-l2）；aclnn 两段式调用（u2-l1）。Kernel UT 的本质，就是**绕过框架，在仿真环境里手工复现「Host 写 tiling → Device 读 tiling 并执行」这条链路**。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [examples/add_example/tests/ut/op_kernel/test_add_example.cpp](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/tests/ut/op_kernel/test_add_example.cpp) | 教学样例的 Kernel UT：手工构造 tiling + 仿真执行的最小骨架 |
| [activation/gelu/tests/ut/op_kernel/test_gelu_apt.cpp](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/tests/ut/op_kernel/test_gelu_apt.cpp) | 生产算子的 Kernel UT：接入公共数据框架的完整姿势 |
| [tests/ut/op_kernel/kernel_ut_data_helper.h](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/tests/ut/op_kernel/kernel_ut_data_helper.h) / [.cpp](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/tests/ut/op_kernel/kernel_ut_data_helper.cpp) | 公共测试数据框架：路径定位、目录拷贝、旧产物清理 |
| [tests/ut/op_kernel/kernel_ut_data_executor.cpp](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/tests/ut/op_kernel/kernel_ut_data_executor.cpp) | 公共测试数据框架：统一拉起 `gen_data.py` / `compare_data.py` |
| [activation/gelu/tests/ut/op_kernel/gelu_data/gen_data.py](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/tests/ut/op_kernel/gelu_data/gen_data.py) | 数据生成脚本：NumPy 造输入、PyTorch 算 golden、落盘 bin |
| [tests/ut/op_kernel/CMakeLists.txt](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/tests/ut/op_kernel/CMakeLists.txt) | Kernel UT 可执行文件的组装与自动运行 |
| [cmake/ut.cmake](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/cmake/ut.cmake) | `AddOpTestCase` 函数：把一个算子的 kernel UT 注册进构建 |
| [activation/gelu/tests/st/aclnnGelu/atk_aclnnGelu.json](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/tests/st/aclnnGelu/atk_aclnnGelu.json) | ST 用例清单：205 条 shape/dtype/数值范围组合 |
| [activation/gelu/tests/st/aclnnGelu/executor_aclnnGelu.py](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/tests/st/aclnnGelu/executor_aclnnGelu.py) | ST 参考实现：用 `torch.nn.GELU` 做对账基准 |
| [activation/gelu/tests/assets/golden.py](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/tests/assets/golden.py) | ST golden 函数注册表与精度参考实现 |
| [tests/requirements.txt](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/tests/requirements.txt) | 测试相关 Python 依赖 |

## 4. 核心概念与源码讲解

### 4.1 Kernel UT 的执行原理：为什么不需要 NPU

#### 4.1.1 概念说明

Kernel UT 要回答的问题是：**「给定输入和 tiling 参数，Device 侧 kernel 的计算逻辑对不对？」**。它与 tiling UT（验证 Host 切分算法）互补：tiling UT 的产物（TilingData 字节、tilingKey、blockDim）恰好是 kernel UT 的输入。

关键在于执行载体：Kernel UT 通过 `tikicpulib`（CANN 包提供的 CPU 仿真库，按 soc 版本链接）把 Ascend C kernel 编译为在 x86 主机上运行的代码，用 `AscendC::GmAlloc` 分配的**仿真 GM 内存**模拟真实 Global Memory，多核则退化为多次函数调用。因此整个测试跑在普通服务器上，速度接近本地单元测试。

#### 4.1.2 核心流程

一个 Kernel UT 用例的通用骨架（与开发指南给出的六步一致）：

```text
1. 设定 shape/dtype，申请输入/输出/workspace/tiling 四块仿真 GM 内存（AscendC::GmAlloc）
2. 准备 TilingData：手工填字段，或复用 tiling UT 的 ExecuteTiling 自动生成
3. ICPU_SET_TILING_KEY(<key>)        ← 告诉仿真器选哪个模板分支
4. AscendC::SetKernelMode(AIV_MODE)  ← 声明矢量核模式
5. ICPU_RUN_KF(kernel, numBlocks, ...) ← 以 numBlocks 个"核"仿真执行入口函数
6. 校验输出（EXPECT_* 或调用 compare_data.py 对账），AscendC::GmFree 释放
```

#### 4.1.3 源码精读

先看教学样例 [test_add_example.cpp](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/tests/ut/op_kernel/test_add_example.cpp) 的两处关键代码。

第一处：**直接 include kernel 源文件**触发模板实例化（add_example 入口是模板函数 `add_example<TILING_KEY>`，include `.cpp` 才会生成可链接的实例）：

- [examples/add_example/tests/ut/op_kernel/test_add_example.cpp:L21-L22](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/tests/ut/op_kernel/test_add_example.cpp#L21-L22)：把 `op_kernel/add_example.cpp` 和 tiling data 头文件直接包含进测试编译单元，同时用 `#ifdef __CCE_KT_TEST__` 把 `tikicpulib.h`、`data_utils.h` 包起来——这些头只在 kernel UT 编译态下可用。

第二处：**仿真执行的三个宏/调用**：

- [examples/add_example/tests/ut/op_kernel/test_add_example.cpp:L57-L63](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/tests/ut/op_kernel/test_add_example.cpp#L57-L63)：先用 lambda 把模板入口 `::add_example<0>` 包装成普通函数（`<0>` 即 tiling key 0，对应 float 分支，见 u4-l1），然后 `ICPU_SET_TILING_KEY(0)` 设置仿真器的 tiling key、`SetKernelMode(AIV_MODE)` 声明矢量核、`ICPU_RUN_KF(AddExampleKernel, numBlocks, ...)` 以 `numBlocks = 8` 个仿真核执行，参数顺序与 kernel 入口签名完全一致（输入、输出、workspace、tiling）。

再看构建侧，理解这条链路如何被组装：

- [cmake/ut.cmake:L397-L404](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/cmake/ut.cmake#L397-L404)：`AddOpTestCase` 的用法注释——参数依次为算子名、支持的 soc 版本（如 `ascend910B1`）、自定义编译选项（如 `-DDTYPE_X=half`），还可跟依赖算子列表。
- [tests/ut/op_kernel/CMakeLists.txt:L26-L33](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/tests/ut/op_kernel/CMakeLists.txt#L26-L33)：为每个 soc 版本生成一个可执行文件 `nn_op_kernel_ut_<soc>`，链接所有算子的用例动态库与 `tikicpulib::<socVersion>`——这就是"仿真库按芯片型号链接"的落点。
- [tests/ut/op_kernel/scripts/run_kernel_ut.sh:L1-L7](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/tests/ut/op_kernel/scripts/run_kernel_ut.sh#L1-L7)（在 [tests/ut/op_kernel/CMakeLists.txt:L54-L76](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/tests/ut/op_kernel/CMakeLists.txt#L54-L76) 中被 POST_BUILD 调用）：编译完成后自动用该脚本执行测试，支持单用例超时（默认 120 秒）、符号化开关与 ASAN 预加载，防止仿真卡死拖垮整个 CI。
- [tests/ut/op_kernel/test_op_kernel_main.cpp:L18-L27](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/tests/ut/op_kernel/test_op_kernel_main.cpp#L18-L27)：全局 Environment 启动时检查 `python3` 是否可用——因为下一节的数据生成/对账要靠 Python 脚本。

#### 4.1.4 代码实践

1. **实践目标**：直观感受「kernel UT 跑在 CPU 上、无需 NPU」。
2. **操作步骤**：
   - 先安装测试依赖：`pip3 install -r tests/requirements.txt`（见 [tests/requirements.txt](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/tests/requirements.txt)，包含 tensorflow、ml-dtypes、en-dtypes）。
   - 执行 `bash build.sh -u --opkernel --ops=add_example`（命令形态见 [docs/zh/install/compile.md:L246-L256](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/install/compile.md#L246-L256)）。
   - 观察输出末尾的 gtest 汇总（`[  PASSED  ] N tests.`，参见 [docs/zh/install/compile.md:L262-L271](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/install/compile.md#L262-L271)）。
   - 用 `--noexec` 再编译一次，到构建目录手动执行 `nn_op_kernel_ut_<soc>` 二进制，确认它是一个普通 x86 可执行程序。
3. **需要观察的现象**：整条命令在没有 NPU 的机器上也能完成编译并执行用例；用例耗时是毫秒级。
4. **预期结果**：add_example 的 `test_case_0` 通过。完整运行输出**待本地验证**。

#### 4.1.5 小练习与答案

- **练习 1**：为什么 test_add_example.cpp 要 `#include "../../../op_kernel/add_example.cpp"`，而不是像普通单元测试那样链接库？
  **答案**：add_example 的入口是带模板参数（tiling key）的模板函数，模板需要显式实例化才会生成代码；直接 include 源文件让测试编译单元自己实例化 `add_example<0>`，免去为测试单独维护实例化清单。生产算子 gelu 的入口是普通 `extern "C"` 函数，因此只需声明原型（见 4.2 的 [test_gelu_apt.cpp:L26](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/tests/ut/op_kernel/test_gelu_apt.cpp#L26)）。
- **练习 2**：`ICPU_RUN_KF` 的第二个参数 `numBlocks = 8` 改成 1 会发生什么？
  **答案**：仿真只按 1 个核执行，kernel 内 `AscendC::GetBlockIdx()` 恒为 0，只会覆盖第一个 `blockFactor` 窗口的数据。仿真器的核数与 TilingData 里 `blockFactor` 的划分需要配套——这正是真实运行时「BlockDim 与核切分一致」约束在 UT 中的体现。

### 4.2 Kernel UT 用例解剖：从手工 tiling 到结果落盘

#### 4.2.1 概念说明

Kernel UT 用例要手工完成框架平时替你做的三件事：填输入数据、算 TilingData、校验输出。教学样例走最简路线（不填输入、不校验输出，只验证"能跑通不越界"）；生产算子则补全了数据生成与结果落盘。对比这两者，就能看出一个"合格"的 Kernel UT 应该长什么样。

#### 4.2.2 核心流程

以 gelu 的用例为例：

```text
SetUp：SetupTestEnvironment(仓库相对数据目录, 本地目录名)
       → 拷贝 gelu_data/ 到执行目录、清理旧 bin、设置权限
用例：  RunGenData("./gelu_data", {"'(256)'", "float32"})
       → 在数据目录里执行 python3 gen_data.py '(256)' float32
       → 产出 input_x.bin（随机输入）与 golden.bin（PyTorch 参考输出）
        GmAlloc 申请 x/y/workspace/tiling 四块仿真内存
        手工填 EleBaseTilingData16B（dim0/coreNum/ubFormer）
        ReadFile 把 input_x.bin 读入仿真 GM（本例省略，直接跑随机内存亦可行）
        ICPU_SET_TILING_KEY(1003) + ICPU_RUN_KF 执行
        WriteFile 把输出 y 落盘为 output.bin
TearDown：CleanGeneratedBinFiles 清理生成的 bin
```

#### 4.2.3 源码精读

- [examples/add_example/tests/ut/op_kernel/test_add_example.cpp:L35-L55](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/tests/ut/op_kernel/test_add_example.cpp#L35-L55)：教学样例的数据准备。按 `32*4*4*4` 个 float 计算三块缓冲的字节数，`GmAlloc` 申请（workspace 给了 16MB 的富余）；然后**手工**填 TilingData 三个字段：`totalNum = 2048`、`blockFactor = 8`、`ubFactor = 8`——注意这两个切分值不必与真实 tiling 算法一致，只要满足 `blockFactor * numBlocks >= totalNum` 且 `ubFactor` 合理即可，因为 UT 只验证 kernel 侧逻辑。
- [activation/gelu/tests/ut/op_kernel/test_gelu_apt.cpp:L38-L65](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/tests/ut/op_kernel/test_gelu_apt.cpp#L38-L65)：生产算子完整用例。L49-L50 调用公共框架准备环境并生成数据；L54-L58 手工填公共 tiling 结构 `EleBaseTilingData16B`（u5-l3 讲过的 `ElewiseBaseTiling` 公共框架）；L62 `ICPU_SET_TILING_KEY(1003)`——1003 是 gelu 的 schMode×dType 二维 tiling key 编码中 FP32 组合的取值；L64 执行后 L65 用 `WriteFile` 把输出落盘，供后续与 `golden.bin` 对账。
- [activation/gelu/tests/ut/op_kernel/gelu_data/gen_data.py:L18-L37](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/tests/ut/op_kernel/gelu_data/gen_data.py#L18-L37)：数据生成脚本本体——解析 shape 字符串、`np.random.uniform` 造输入、用 `torch.nn.GELU(approximate="tanh")` 算 golden，最后 `tofile` 落成 `input_x.bin` 与 `golden.bin`。**golden 与被测 kernel 必须用同一个数学定义**（这里都是 tanh 近似），否则对账永远失败。
- [activation/gelu/tests/ut/op_kernel/CMakeLists.txt:L12](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/tests/ut/op_kernel/CMakeLists.txt#L12)：`AddOpTestCase(gelu "ascend950pr_9599" "-DDTYPE_X=half")`——注意 soc 版本必须与该算子 kernel 实际交付的芯片一致（gelu 的 arch35 目录只编给 ascend950 系列，见 u5-l3），编译选项用于圈定一种典型 dtype 组合。

#### 4.2.4 代码实践

1. **实践目标**：读懂 gelu 用例中「Python 造数 → C++ 仿真 → bin 落盘」的三段衔接。
2. **操作步骤**：
   - 单独运行数据脚本：`cd activation/gelu/tests/ut/op_kernel/gelu_data && python3 gen_data.py '(256)' float32`。
   - 用 `ls` 确认生成了 `input_x.bin` 与 `golden.bin`（各 1024 字节）。
   - 执行 `bash build.sh -u --opkernel --ops=gelu`，观察日志中 `RunGenData` 的调用与用例结果。
   - 检查执行目录下是否出现 `gelu_data/output.bin`。
3. **需要观察的现象**：三个 bin 文件字节数一致；output.bin 中的 float 值与 golden.bin 接近（可用 `python3` 读回粗略比对）。
4. **预期结果**：用例通过且三个文件生成。注意本例的 C++ 侧没有把 `input_x.bin` 读入仿真内存、也没有自动比对（比对脚本 `compare_data.py` 在 gelu 中未提供，其他算子如 fatrelu_mul 有），因此 output.bin 是对「未初始化输入」的计算结果，**严格对账需按 4.3 的框架补 ReadFile 与 RunCompareData**。完整行为待本地验证。

#### 4.2.5 小练习与答案

- **练习 1**：test_add_example.cpp 没有填输入数据也没校验输出，它到底测了什么？
  **答案**：它测的是**执行链路与内存安全**——tiling 契约能否被 kernel 正确消费、`blockFactor/ubFactor` 驱动的主循环与尾块处理是否越界、模板分支能否实例化并跑完。配合 ASAN（run_kernel_ut.sh 支持 preload libasan）可以捕获越界读写。但它不验证数值正确性，这正是教学样例与生产用例的差距。
- **练习 2**：gelu 用例中 `ICPU_SET_TILING_KEY(1003)` 与 lambda 里的 `::gelu<0, TPL_FP32>` 是什么关系？
  **答案**：lambda 里显式指定了模板实参 `<0, TPL_FP32>`（schMode=0、dtype=FP32），决定**编译哪个实例**；`ICPU_SET_TILING_KEY(1003)` 告诉仿真器运行期的 tiling key 值。真实运行时框架按 Host 侧 `SetTilingKey` 的值选择二进制，UT 中这两处必须人工对齐到同一组合，否则测的不是你以为的分支。

### 4.3 公共测试数据框架：kernel_ut_data_helper 与 executor

#### 4.3.1 概念说明

如果每个用例都自己 `system("cp -r ...")`、`system("python3 gen_data.py ...")`，路径拼错、旧产物残留、权限问题会反复出现。ops-nn 把这些碎活收敛成 `tests/ut/op_kernel/` 下的公共框架：**helper 管文件系统**（拷贝数据目录、清理生成的 bin、定位仓库根），**executor 管脚本拉起**（统一调用 `gen_data.py`/`compare_data.py`/`gen_tiling.py` 并检查退出码）。用例代码因此只剩业务逻辑。

#### 4.3.2 核心流程

```text
SetupTestEnvironment(dataDirRelPath, localName)
  └─ PrepareTestDataDir：把 仓库内<算子>/tests/ut/op_kernel/<xx>_data 拷到本地执行目录
  └─ 清理上次生成的 *.bin、修正目录权限
RunGenData(dir, args)   → cd dir && python3 gen_data.py args（存在性检查 + 退出码检查）
ICPU_RUN_KF(...)        → 仿真执行 kernel
RunCompareData(dir, args) → cd dir && python3 compare_data.py args（读 output.bin 与 golden.bin 比对）
```

#### 4.3.3 源码精读

- [tests/ut/op_kernel/kernel_ut_data_helper.h:L21-L37](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/tests/ut/op_kernel/kernel_ut_data_helper.h#L21-L37)：helper 的全部对外接口——`GetRepoRootDir`/`GetTestWorkDir` 定位路径，`PrepareTestDataDir`/`CleanGeneratedBinFiles` 管目录与产物，`SetupTestEnvironment` 是前几者的一站式封装。
- [tests/ut/op_kernel/kernel_ut_data_helper.cpp:L224-L238](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/tests/ut/op_kernel/kernel_ut_data_helper.cpp#L224-L238)：`SetupTestEnvironment` 内部先调 `PrepareTestDataDir`（定义于 [L191](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/tests/ut/op_kernel/kernel_ut_data_helper.cpp#L191)），把数据目录从仓库拷到测试工作目录——这样生成/比对产生的中间文件不会污染源码树，这也是 gelu 用例 `TearDown` 里 `CleanGeneratedBinFiles("./gelu_data")` 清理的对象。
- [tests/ut/op_kernel/kernel_ut_data_executor.cpp:L63-L88](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/tests/ut/op_kernel/kernel_ut_data_executor.cpp#L63-L88)：`RunGenData` 先检查 `gen_data.py` 是否存在，再拼出 `python3 gen_data.py <args...>` 并 `cd` 到数据目录执行，退出码非零时打日志并返回 false——用例里应使用 `ASSERT_TRUE(RunGenData(...))` 让失败尽早暴露。
- [tests/ut/op_kernel/kernel_ut_data_executor.cpp:L90-L116](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/tests/ut/op_kernel/kernel_ut_data_executor.cpp#L90-L116)：`RunCompareData` 与之对称，拉起 `compare_data.py`；比对脚本本身由各算子自带（如 [activation/fatrelu_mul/tests/ut/op_kernel/fatrelu_mul_data/compare_data.py](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/fatrelu_mul/tests/ut/op_kernel/fatrelu_mul_data/compare_data.py)，开发指南 [docs/zh/develop/aicore_develop_guide.md:L712-L722](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/develop/aicore_develop_guide.md#L712-L722) 给出了参考位置）。

#### 4.3.4 代码实践

1. **实践目标**：为 4.2 中发现的缺口补一条严格对账路径（源码阅读型实践）。
2. **操作步骤**：
   - 阅读 [docs/zh/develop/aicore_develop_guide.md:L734-L770](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/develop/aicore_develop_guide.md#L734-L770) 的「使用公共测试数据框架」示例。
   - 参照 fatrelu_mul 的 `compare_data.py`，写出 gelu 版伪代码：读 `golden.bin` 与 `output.bin` → `np.allclose(..., atol=1e-5)` → 失败时打印首个不匹配元素的下标与两侧值。
   - 在 test_gelu_apt.cpp 的 `ICPU_RUN_KF` 之后补两步：用 `data_utils.h` 的 `ReadFile` 把 `input_x.bin` 灌入 x（需在执行前）、`ASSERT_TRUE(kernel_ut::RunCompareData("./gelu_data", {"float32"}))`。
3. **需要观察的现象**：人为把 golden 计算改成 `approximate="none"` 再跑，compare 应当报出大量失配，证明对账真的在工作。
4. **预期结果**：修改属于练习性质，运行结果**待本地验证**。

#### 4.3.5 小练习与答案

- **练习 1**：为什么数据目录要整体拷贝到执行目录，而不是直接在源码目录里生成 bin？
  **答案**：一是并行/多次执行互不干扰、不污染源码树；二是 `TearDown` 的 `CleanGeneratedBinFiles` 可以放心地按通配清理整个目录而不会误删仓库文件；三是构建目录可能只读。`SetupTestEnvironment` 的第二个参数（本地目录名）就是拷贝后的名字。
- **练习 2**：`RunGenData` 内部用 `system()` 执行命令，为什么文档还推荐用它而不是自己在用例里 `system("python3 ...")`？
  **答案**：框架统一做了脚本存在性检查、工作目录切换、stdout/stderr 重定向与退出码判断，并输出带 `[KernelUTExecutor]` 前缀的日志，失败可追溯；散落的裸 `system()` 这些都没有，排障成本高。

### 4.4 ST 系统测试与 UT/ST 分工

#### 4.4.1 概念说明

UT（含 kernel 仿真）验证的是**交付件代码逻辑**，跑在 x86 上，快而廉价；ST（System Test，系统测试）则把算子包安装到**真实 NPU 环境**，通过 aclnn/图模式走完整调用链，与参考实现对精度（必要时也对性能）对账。两者是互补关系，不是替代关系：

| 维度 | Kernel UT | ST |
|------|-----------|-----|
| 执行环境 | x86 仿真（tikicpulib），无需 NPU | 真实 NPU |
| 被测对象 | kernel 源文件（直接 include） | 安装后的算子包 + aclnn/图模式全链路 |
| 数据规模 | 少量手工/脚本构造的用例 | 大规模 shape/dtype/边界值矩阵 |
| 参考实现 | gen_data.py 内嵌 PyTorch 计算 | executor 注册的参考 API + golden.py |
| 典型用途 | 开发期快速迭代、回归防越界 | 交付验收、精度认证、模糊测试 |

ops-nn 的 ST 交付件放在 `<算子>/tests/st/` 下，主要由三类文件组成：**用例清单 json**、**参考实现 executor**、**golden 函数**；部分算子还有 **fuzz 用例表 csv**。

#### 4.4.2 核心流程

```text
用例清单 atk_aclnnGelu.json：每条声明 name/torch 参考名、aclnn_name、api_type、
    每个输入的 dtype/shape/range_values（如 [-7,7]、"nan"、"inf"）
        ↓ 测试框架（atk）按清单生成输入张量
在 NPU 上调用 aclnnGelu（真实算子包）
        ↓ 同时调用 executor 注册的参考实现
executor_aclnnGelu.py：torch.nn.GELU(approximate='tanh') 计算期望输出
        ↓ 精度对账（按 standard.acc 与容差判定）
golden.py：__golden__ 注册表提供 kernel 级 golden 函数（ST 与仿真共用同一数学定义）
```

#### 4.4.3 源码精读

- [activation/gelu/tests/st/aclnnGelu/atk_aclnnGelu.json:L1](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/tests/st/aclnnGelu/atk_aclnnGelu.json#L1)：单行 JSON 数组，共 205 条用例。每条包含 `name`（参考实现 `torch.nn.GELU`）、`aclnn_name`（`Gelu`）、`api_type`（`aclnn_gelu`，即 executor 的注册名）、`inputs[].dtype/shape/range_values` 与 `standard`（精度/性能判定标准）。注意 range_values 覆盖了 `"nan"`、`"inf"`、`"-inf"` 等边界值，且 shape 从 `[1]` 一直到 `[1024,1024,3,5]`、`[4,2340,2560]` 这类真实模型规模。
- [activation/gelu/tests/st/aclnnGelu/executor_aclnnGelu.py:L20-L30](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/tests/st/aclnnGelu/executor_aclnnGelu.py#L20-L30)：`@register("aclnn_gelu")` 把 `TorchGelu` 注册为该 api_type 的参考实现，`__call__` 里用 `torch.nn.GELU(approximate='tanh')` 计算期望输出——**参考实现的近似方式必须与 kernel 实现一致**，这与 gen_data.py 的约束同源。
- [activation/gelu/tests/assets/golden.py:L15-L46](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/tests/assets/golden.py#L15-L46)：`__golden__` 字典把 kernel 名映射到 golden 函数；`gelu_golden` 接收 numpy 输入，先把 float16/bfloat16 提升为 float32 计算再转回原 dtype——与 kernel 侧「升 float 计算」的精度策略对应（u5-l3）。函数注释明确"参数名与顺序跟随 `gelu_def.cpp` 且不含输出"，即 **golden 的签名契约对齐 def 文件**。
- [activation/gelu/tests/st/arch35/ttk_kernel_gelu_st.csv:L1-L5](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/tests/st/arch35/ttk_kernel_gelu_st.csv#L1-L5)：fuzz 用例表，每行一个 `gelu_fuzz_*` 用例，声明输入/输出的 dtype、shape、format、数值范围（含 `nan`、`-0.0`、`inf`）与精度容差（`absolute_precision = 1e-08`）——这是对 json 清单的补充，专测边界与异常值。
- [tests/requirements.txt:L1-L3](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/tests/requirements.txt#L1-L3)：tensorflow、ml-dtypes、en-dtypes 是测试链路（含 ST 侧数据构造与扩展 dtype 支持）的 Python 依赖，[docs/zh/install/compile.md:L240-L242](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/install/compile.md#L240-L242) 要求先 `pip3 install -r tests/requirements.txt`。
- 关于 ST 的具体执行命令：仓库文档未给出直接驱动 `tests/st/` 的 build.sh 参数，ST 由独立的 atk 测试框架在 CI 中消费这些交付件，**执行入口待确认**（可关注仓库 CI 配置或 atk 框架文档）。

#### 4.4.4 代码实践（源码阅读型）

1. **实践目标**：建立一个算子的「UT/ST 测试资产清单」的概念。
2. **操作步骤**：
   - 列出 gelu 的全部测试文件：`find activation/gelu/tests -type f`。
   - 对照本讲内容给每个文件归类：infershape UT / tiling UT / kernel UT / ST 清单 / ST executor / golden / fuzz 表。
   - 挑 atk_aclnnGelu.json 中 id=73（`range_values: ["nan"]`）与 id=201（shape `[10,257,6144]`）两条用例，思考它们分别防什么问题（NaN 传播、大 shape 的多核切分与尾块）。
3. **需要观察的现象**：一个生产算子的测试资产远多于教学样例（add_example 只有 ut 无 st），且 ST 侧 shape 覆盖呈"小边界 + 大真实规模"的两头分布。
4. **预期结果**：整理出一张七类文件的对照表（本实践无需运行环境，纯阅读即可完成）。

#### 4.4.5 小练习与答案

- **练习 1**：为什么 ST 的参考实现（executor/golden）与 kernel UT 的 gen_data.py 都强调 `approximate='tanh'`？
  **答案**：GELU 有精确式与 tanh 近似两种数学定义，数值差异可达 1e-3 量级。参考实现与被测 kernel 必须采用同一定义，否则精度对账会系统性失败；这也提醒我们：**读一个算子的测试资产前，先读它的数学定义约定**。
- **练习 2**：如果你只能为一个新算子保留三条测试，选哪三条？
  **答案**：典型参考——① 一条 tiling UT（覆盖最常见 shape，锁定 TilingData/blockDim 契约）；② 一条 kernel UT（手工 tiling + gen_data/compare 对账，锁定计算正确性与内存安全）；③ 一条最小 ST（真机 aclnn 调用 + 单 dtype 单 shape 对账，锁定集成链路）。边界值与大 shape 矩阵可后续以 st json/fuzz 表低成本扩充。

## 5. 综合实践

**任务：为 u1-l4 中修改过的 Mul 版 AddExample 补一个带精度校验的 kernel UT，并跑通。**

前提：已按 u1-l4 把 [examples/add_example/op_kernel/add_example.h](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example.h) 中 `AscendC::Add` 改为 `AscendC::Mul`。

步骤：

1. 在 [examples/add_example/tests/ut/op_kernel/test_add_example.cpp](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/tests/ut/op_kernel/test_add_example.cpp) 中新增一个用例（**示例代码**，可直接追加到文件末尾）：

   ```cpp
   TEST_F(add_example_test, test_case_mul_verify)
   {
       constexpr uint32_t kNum = 64;  // 64 个 float，满足 32B 对齐
       size_t byteSize = kNum * sizeof(float);
       uint8_t* x = (uint8_t*)AscendC::GmAlloc(byteSize);
       uint8_t* y = (uint8_t*)AscendC::GmAlloc(byteSize);
       uint8_t* z = (uint8_t*)AscendC::GmAlloc(byteSize);
       uint8_t* workspace = (uint8_t*)AscendC::GmAlloc(1024 * 1024);
       auto* td = reinterpret_cast<AddExampleTilingData*>(
           AscendC::GmAlloc(sizeof(AddExampleTilingData)));
       td->totalNum = kNum;
       td->blockFactor = kNum;  // 单核一次搬完
       td->ubFactor = kNum;

       float* fx = reinterpret_cast<float*>(x);
       float* fy = reinterpret_cast<float*>(y);
       for (uint32_t i = 0; i < kNum; i++) { fx[i] = 1.5f * i; fy[i] = 0.5f; }

       auto runKernel = [](GM_ADDR x, GM_ADDR y, GM_ADDR z, GM_ADDR ws, GM_ADDR t) {
           ::add_example<0>(x, y, z, ws, t);
       };
       ICPU_SET_TILING_KEY(0);
       AscendC::SetKernelMode(KernelMode::AIV_MODE);
       ICPU_RUN_KF(runKernel, 1, x, y, z, workspace, (uint8_t*)td);

       float* fz = reinterpret_cast<float*>(z);
       for (uint32_t i = 0; i < kNum; i++) {
           EXPECT_NEAR(fz[i], 1.5f * i * 0.5f, 1e-6f);  // 乘法期望值
       }
       AscendC::GmFree(x); AscendC::GmFree(y); AscendC::GmFree(z);
       AscendC::GmFree(workspace); AscendC::GmFree((uint8_t*)td);
   }
   ```

   注意要点：`blockFactor * 核数(1) == totalNum` 保证数据被完整覆盖；期望值 `x*y` 是逐元素乘积；`EXPECT_NEAR` 给浮点容差。
2. 无需改 CMake——用例文件在 `tests/ut/op_kernel/` 下按文件名通配自动收集（[examples/add_example/tests/ut/op_kernel/CMakeLists.txt:L16-L18](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/tests/ut/op_kernel/CMakeLists.txt#L16-L18) 的 `AddOpTestCase(add_example "ascend910B1" "")` 已覆盖整个目录）。
3. 执行 `pip3 install -r tests/requirements.txt`（首次），然后 `bash build.sh -u --opkernel --ops=add_example`。
4. 观察输出中 `add_example_test.test_case_mul_verify` 是否 PASSED。
5. 反向验证：把期望值故意改回加法 `1.5f*i + 0.5f` 再跑一次，确认用例**失败**且 gtest 打印出失配的元素——一个从未失败过的断言是不可信的。

预期结果：步骤 4 通过、步骤 5 失败。完整运行输出**待本地验证**。

## 6. 本讲小结

- Kernel UT 通过 `tikicpulib` 在 x86 主机上仿真执行 Ascend C kernel，**无需 NPU**；`ICPU_RUN_KF`（执行）、`ICPU_SET_TILING_KEY`（选模板分支）、`AscendC::GmAlloc/GmFree`（仿真 GM）是三个核心原语。
- 用例骨架六步：申请内存 → 准备 TilingData（手工填或复用 `ExecuteTiling`）→ 设 tiling key 与 kernel mode → 仿真执行 → 校验/落盘 → 释放；模板入口需直接 include kernel `.cpp` 触发实例化。
- 公共数据框架 `kernel_ut_data_helper/executor` 把「拷贝数据目录、拉起 gen_data.py、拉起 compare_data.py」标准化，配合 `data_utils.h` 的 `ReadFile/WriteFile` 构成「Python 造数 + C++ 仿真 + Python 对账」闭环；参考实现必须与 kernel 使用同一数学定义。
- `AddOpTestCase(算子名, soc版本, 编译选项)` 把算子注册进 kernel UT 构建，构建后由 `run_kernel_ut.sh` 自动执行（带超时、符号化与 ASAN 支持）。
- ST 在真实 NPU 上走 aclnn/图模式全链路，交付物是用例清单 json（atk）、参考实现 executor、golden 函数与 fuzz csv；UT 管逻辑与内存安全、ST 管精度与集成，二者互补。
- 为新算子规划最小测试集：tiling UT + 带对账的 kernel UT + 一条最小 ST，再按需扩充 shape/dtype/边界值矩阵。

## 7. 下一步学习建议

本讲完成了 u7 测试体系单元。建议接下来：

1. 进入 u8 单元学习调试与性能：先读 u8-l1（printf、DumpTensor 与 Host 日志），把「UT 跑通但数值不对」时的定位手段补上。
2. 对照阅读两个高质量参考实现：[activation/fatrelu_mul/tests/ut/op_kernel/fatrelu_mul_data/compare_data.py](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/fatrelu_mul/tests/ut/op_kernel/fatrelu_mul_data/compare_data.py)（完整对账脚本）与 [activation/clipped_swiglu/tests/ut/op_kernel/clipped_swiglu_data/gen_data.py](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/clipped_swiglu/tests/ut/op_kernel/clipped_swiglu_data/gen_data.py)（多输入造数）。
3. 通读 [docs/zh/develop/aicore_develop_guide.md](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/develop/aicore_develop_guide.md) 的「算子验证」章节（L452 起），把 infershape/tiling/kernel 三类 UT 的官方模板与本讲实例互相印证。
