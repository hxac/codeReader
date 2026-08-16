# 测试体系：ST 用例结构与运行脚本

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 PTO ST（System Test）用例的「四件套」——`<name>_kernel.cpp`、`main.cpp`、`gen_data.py`、`CMakeLists.txt`——各自的职责与相互约定。
2. 独立读懂任意一条指令的 ST 用例，并理解 golden 对拍（期望数据比对）的完整数据流。
3. 会用 `tests/script/run_st.py`（NPU/CAMS 仿真）与 `tests/run_cpu.py`（CPU 仿真）定向运行单个用例、单个 gtest case。
4. 理解 `validate_op_coverage.py` 与 `validate_testcase_names.py` 两个静态校验脚本如何守住「用例目录 ↔ run_st.sh 清单 ↔ TEST_F 命名」三方一致性。
5. 能仿照 `tadd` 从零新建一个可跑通的 ST 用例目录。

## 2. 前置知识

本讲建立在你已完成的认知之上（对应讲义 u1-l3、u3-l4），先回顾几个关键概念：

- **ST（System Test）**：区别于单元测试，ST 是「端到端」验证——把输入数据经 PTO 指令序列跑一遍，与独立计算的期望结果（golden）比对。在 PTO 仓库里，**一条指令对应一个 ST 用例目录**（如 `tadd`、`tmatmul`），也有少数跨指令用例（如 `tflashattn`）。
- **`__CPU_SIM` 宏**：编译期后端路由开关。CPU ST 工程在顶层 CMake 里统一定义它，使同一份 kernel 源码编译到 CPU 仿真后端（见 u2-l4）。
- **golden 对拍**：用 Python（numpy）独立实现一份「标准答案」，写进 `golden.bin`；C++ 侧 kernel 输出写进 `output.bin`；gtest 用例里逐元素比对。两侧算法必须独立实现，否则失去验证意义。
- **gtest**：Google 的 C++ 测试框架。一个可执行文件里包含若干 `TEST_F(套件名, 用例名)`，可用 `--gtest_filter=套件名.用例名` 只跑指定用例——这是定向运行的基础。
- **CPU 仿真只验功能**（u3-l4）：事件同步在 CPU 侧是空桩、单线程按序执行，所以 ST 的 CPU 路径验证的是指令语义正确性，不验证流水线时序。

还需要两个本讲新术语：

- **CAMS / sim 模式**：昇腾的硬件级仿真器（camodel）。`run_st.py -r sim` 把 NPU 版用例放到 CAMS 上跑，无需真卡但需要完整 CANN 环境；`-r npu` 则上真机。
- **testcase 注册表**：用例目录本身不会被自动发现，必须登记进 `tests/cpu/st/testcase/CMakeLists.txt` 的 `ALL_TESTCASES` 列表（NPU 侧同理），否则不参与编译。

## 3. 本讲源码地图

| 文件 / 目录 | 作用 |
|---|---|
| `tests/README.md` | 测试体系总入口文档：入口脚本、目录布局、通信测试说明 |
| `tests/cpu/st/testcase/tadd/` | CPU 侧 tadd 用例「四件套」标本 |
| `tests/cpu/st/testcase/CMakeLists.txt` | CPU ST 用例注册表 + `pto_cpu_sim_st()` 构建函数 |
| `tests/cpu/st/CMakeLists.txt` | CPU ST 顶层工程：定义 `__CPU_SIM`、拉 GTest |
| `tests/common/test_common.h` | 测试公共库：`ReadFile`/`WriteFile`/`ResultCmp` 等 |
| `tests/cpu/st/utils.py` | 造数工具 `NumExt`：dtype 映射与 bf16 位操作 |
| `tests/script/run_st.py` | NPU/CAMS ST 的构建 + 运行脚本（本讲主角） |
| `tests/run_cpu.py` | CPU 仿真总入口（单用例定向运行见本讲 4.2） |
| `tests/script/all_cpu_tests.py` | CPU ST 批量构建 + 并行造数 + 批量运行 |
| `tests/validate_op_coverage.py` | 校验 run_st.sh 是否覆盖所有用例目录 |
| `tests/validate_testcase_names.py` | 校验脚本中引用的 gtest case 名真实存在 |
| `tests/npu/a2a3/src/st/testcase/CMakeLists.txt` | NPU 侧用例构建函数 `pto_vec_st`/`pto_cube_st`（对照） |

## 4. 核心概念与源码讲解

### 4.1 用例结构：一个目录、一条指令、四个文件

#### 4.1.1 概念说明

PTO 的 ST 组织遵循一条铁律：**`tests/cpu/st/testcase/` 下一个目录 = 一条指令 = 一个独立的 gtest 可执行文件**。该目录下共 131 个用例目录，覆盖 `tload`、`tstore`、`tadd` 到 `tmatmul_mx` 等绝大多数指令（u1-l2 的「指令 × 后端 × 架构」三维地图在测试侧的投影）。

每个目录固定包含四个文件，俗称「四件套」：

| 文件 | 编译目标 | 职责 |
|---|---|---|
| `<name>_kernel.cpp` | 设备侧代码（CPU 仿真下与 main 同进程） | 写 PTO 指令序列，即「被测对象」 |
| `main.cpp` | gtest 可执行 | host 侧编排：申请内存、喂数据、调 kernel、比对 golden |
| `gen_data.py` | 不编译，由 Python 执行 | 生成 `input*.bin` 与 `golden.bin` 期望数据 |
| `CMakeLists.txt` | 构建脚本 | 通常只有一行：调用注册函数 |

这套约定的价值在于**极致的复制性**：新增一条指令的测试 = 复制 `tadd` 目录、改指令名与 golden 公式、登记注册表，三步完成。u1-l4 你已经把 Add 的 NPU 版 kernel 读过一遍，本节把同一指令的 CPU ST 标本完整拆开。

#### 4.1.2 核心流程

一个 ST 用例的完整生命周期（以 tadd 为例）：

```text
① 构建期
   run_st.py / run_cpu.py 调 cmake -DTEST_CASE=tadd
   → testcase/CMakeLists.txt 的 foreach 命中 tadd
   → pto_cpu_sim_st(tadd) 生成可执行文件 build/bin/tadd

② 造数期（每个 gtest case 一次）
   gen_data.py 在 build 目录下运行
   → 为每个 case 建 "TADDTest.case_xxx" 子目录
   → 写 input1.bin / input2.bin / golden.bin

③ 运行期
   ./tadd [ --gtest_filter=... ]  （工作目录 = build/bin/）
   每个 TEST_F 内部：
     读 ../TADDTest.case_xxx/input1.bin、input2.bin
     → 拷入 device 内存（CPU 仿真下是宿主普通内存）
     → LaunchTAdd 执行指令序列，输出写回 output.bin
     → 读 golden.bin 与 output.bin 逐元素比对 → PASS/FAIL
```

关键约定有两条，违反任何一条用例都会「找不到数据」：

- **目录名 = 可执行名**：`pto_cpu_sim_st(tadd)` 要求目录里必须有 `main.cpp`，可执行文件名即目录名。
- **golden 目录名 = `<套件名>.<用例名>`**：`gen_data.py` 生成的子目录名必须与 `main.cpp` 里的 `TEST_F(TADDTest, case_...)` 完全一致，因为 `main.cpp` 是用 gtest 运行时 API 反查当前 case 名再拼出 `../` 相对路径的。

#### 4.1.3 源码精读

**(a) kernel 侧：指令序列的「最小投影」**

[tadd_kernel.cpp:L16-L43](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/tadd_kernel.cpp#L16-L43) 是被测主体：一个模板函数 `runTAdd`，把 GlobalTensor/Tile/事件/指令的标准骨架（u1-l4、u2 系列）压缩到 28 行。

其中三个片段值得注意：

- [tadd_kernel.cpp:L22-L28](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/tadd_kernel.cpp#L22-L28)：三个 Vec tile 用 `TASSIGN` 手工摆到 `0x0 / 0x4000 / 0x8000` 三个片上偏移——Manual 模式下缓冲排布是开发者的责任（u3-l2），ST 用例选了最朴素的等距摆放。
- [tadd_kernel.cpp:L34-L41](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/tadd_kernel.cpp#L34-L41)：`TLOAD → set/wait_flag(MTE2→V) → TADD → set/wait_flag(V→MTE3) → TSTORE`，正是 u2-l3 讲过的生产者挂牌、消费者等牌事件协议。CPU 仿真下这些 flag 是空桩（u3-l4），所以本用例在 CPU 上验证的纯粹是 TADD 的语义。
- [tadd_kernel.cpp:L54-L62](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/tadd_kernel.cpp#L54-L62)：**显式模板实例化**。`main.cpp` 只声明模板（见下），真正实例化哪些 dtype/形状组合由 kernel 文件末尾这几行决定——这是「host 与 device 分离编译」的粘合点，新增 dtype 组合时必须在这里补一行。

**(b) main 侧：host 编排与对拍**

- [main.cpp:L27-L34](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/main.cpp#L27-L34)：`GetGoldenDir()` 用 `testing::UnitTest::GetInstance()->current_test_info()` 反查当前套件名与用例名，拼出 `../TADDTest.case_xxx`。这就是「golden 目录名必须与 TEST_F 命名一致」的机制来源——目录名不是配置项，是运行时算出来的。
- [main.cpp:L60-L70](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/main.cpp#L60-L70)：读入两个输入 → 拷贝到 device → `LaunchTAdd` → 同步 → 结果写 `output.bin`。注意 `WriteFile(GetGoldenDir() + "/output.bin", ...)` 把实际输出写回 golden 目录，方便失败时人工 diff 三份 bin 文件。
- [main.cpp:L83-L90](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/main.cpp#L83-L90)：把 `golden.bin` 和 `output.bin` 都读进 `std::vector`，交给公共库的 `ResultCmp<T>(golden, devFinal, 0.001f)` 判定。
- [main.cpp:L93-L100](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/main.cpp#L93-L100)：四个 `TEST_F`（float/int32/int16/half）加一个 BF16 条件编译 case。case 命名携带形状信息 `case_<dtype>_<全局行x列>_<tile行x列>`，人看日志即可定位参数组合。

**(c) 造数侧：golden 的独立实现**

- [gen_data.py:L21-L38](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/gen_data.py#L21-L38)：用 numpy 造两个随机矩阵，`golden = input1 + input2` 后按有效区（valid_row/valid_col）裁剪——注意 golden 公式独立于 kernel 实现，这正是对拍的意义；TSTORE 只写有效区（u3-l1），所以无效区保持 0。
- [gen_data.py:L53-L64](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/gen_data.py#L53-L64)：`generate_case_name()` 按 `<套件>.case_<dtype>_<尺寸>` 规则拼目录名——**这个函数的输出必须与 main.cpp 的 TEST_F 名字逐字符一致**，本讲 4.3 的校验脚本查的就是这类漂移。
- [gen_data.py:L76-L83](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/gen_data.py#L76-L83)：case 参数表。列表里每一项都要在 kernel 的显式实例化与 main 的 TEST_F 里各有一份对应——四件套通过「dtype × 形状」参数表隐式对齐。
- [utils.py:L17-L76](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/utils.py#L17-L76)：`NumExt` 工具类。核心是解决 numpy 没有 bf16 的问题：[utils.py:L50-L61](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/utils.py#L50-L61) 用位操作（`bits + 0x7FFF + lsb >> 16`，即舍入到最近偶数）手工完成 fp32↔bf16 转换；[utils.py:L64-L76](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/utils.py#L64-L76) 提供 dtype 到短名（`float`/`half`/`int8`…）的映射，供 case 命名复用。

**(d) 注册侧：一行 CMake 的背后**

- [tadd/CMakeLists.txt:L11](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/CMakeLists.txt#L11)：整个文件有效内容只有 `pto_cpu_sim_st(tadd)` 一行。
- [testcase/CMakeLists.txt:L11-L35](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/CMakeLists.txt#L11-L35)：`pto_cpu_sim_st()` 函数把 `main.cpp`（必有）与 `<name>_kernel.cpp`（存在才加入，见 L13-L15）编成一个可执行文件，include 路径指向仓库 `include/`、CPU 桩目录 `stubs/` 与公共测试头 `../../common`（即 `test_common.h`），并链接 gtest_main。
- [testcase/CMakeLists.txt:L172-L176](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/CMakeLists.txt#L172-L176)：`foreach` 遍历 `ALL_TESTCASES`（[L39-L170](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/CMakeLists.txt#L39-L170)，`tadd` 在 L46、`tmul` 在 L105），仅当 `TEST_CASE` 变量未定义或等于当前名时才 `add_subdirectory`——这就是「只编译一个用例」的编译期过滤器。
- [cpu/st/CMakeLists.txt:L31](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/CMakeLists.txt#L31)：工程级 `add_definitions(-D__CPU_SIM)`，整个 CPU ST 树的所有翻译单元都被路由到 CPU 仿真后端。

**(e) 公共对拍库**

- [test_common.h:L64-L101](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/common/test_common.h#L64-L101) 与 [L103-L128](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/common/test_common.h#L103-L128)：`ReadFile`/`WriteFile` 二进制文件读写，所有用例共用。
- [test_common.h:L231-L309](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/common/test_common.h#L231-L309)：`ResultCmp` 容差比对。判定条件是「绝对误差与相对误差同时超阈值才算错」（L259-L261），错误容忍数阈值默认取 \( \lfloor N \cdot \epsilon \rfloor \)（L236，N 为元素总数、ε 为容差），并统计「期望非零、实际为零」的 zero count（防「全零输出假通过」）。整型精确比对走 [L213-L229](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/common/test_common.h#L213-L229) 的 `ResultCmpExact`。

**(f) NPU 侧对照：同样的四件套，不同的构建函数**

NPU 用例目录（如 `tests/npu/a2a3/src/st/testcase/tadd/`）四个文件一模一样，只是注册函数换成了 [pto_vec_st(tadd)](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/npu/a2a3/src/st/testcase/tadd/CMakeLists.txt#L11)。区别在 [testcase/CMakeLists.txt:L11-L35](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/npu/a2a3/src/st/testcase/CMakeLists.txt#L11-L35)：kernel 编成**独立共享库**（L12，用毕昇编译器 `-xcce` + `--cce-aicore-arch=dav-c220-vec`），host 的 `main.cpp` 编成可执行（L21）再链接 kernel 库；链接项随运行模式切换（L35）——`sim` 链 CAMS 仿真库 `runtime_camodel`，`npu` 链真机 `runtime`。Vec 指令用 `pto_vec_st`，Cube 指令用 `pto_cube_st`（L42 起，架构参数换成 `dav-c220-cube`），另有混核 `pto_mix_st`（L72 起）。编译器与 CCE 选项在 [npu/a2a3/src/st/CMakeLists.txt:L33-L37](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/npu/a2a3/src/st/CMakeLists.txt#L33-L37) 与 [L76-L95](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/npu/a2a3/src/st/CMakeLists.txt#L76-L95) 统一设置（含 `DEBUG_MODE` 的 `--cce-enable-print` 与 `AUTO_MODE` 的 `--cce-pto-auto-enable`）。

#### 4.1.4 代码实践：验证「四件套齐备性」

这是一个纯源码阅读型实践，不需要编译环境。

1. **实践目标**：用事实确认「一个目录一条指令 + 四件套」的约定在仓库中的实际覆盖率。
2. **操作步骤**：
   ```bash
   # 统计 CPU ST 用例目录数
   ls tests/cpu/st/testcase/ | grep -v CMakeLists | grep -v utils | wc -l
   # 检查每个目录是否都有 main.cpp 与 gen_data.py
   for d in tests/cpu/st/testcase/*/; do
     [ -f "$d/main.cpp" ] || echo "缺 main.cpp: $d"
     [ -f "$d/gen_data.py" ] || echo "缺 gen_data.py: $d"
   done
   # 看看有多少用例的 CMakeLists 真的只有一行注册
   grep -L "pto_cpu_sim_st" tests/cpu/st/testcase/*/CMakeLists.txt
   ```
3. **需要观察的现象**：目录计数应约为 131（与 `ALL_TESTCASES` 列表长度一致）；第二条命令几乎无输出；第三条命令应无输出（所有 CMakeLists 都用了注册函数）。
4. **预期结果**：四件套约定高度一致，例外极少。若有输出，记下目录名——那是要么特殊、要么过时的用例，值得单独读一读。
5. 本实践只读不写，可直接执行验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `main.cpp` 只写模板声明 `void LaunchTAdd(...)` 就能链接成功？如果给 `TEST_F` 新增一个 `double` 类型的 case，只改 `main.cpp` 和 `gen_data.py` 会发生什么？

**答案**：`tadd_kernel.cpp` 末尾有显式模板实例化（L54-L62），编译期就生成了特定 dtype/形状组合的符号，链接期由链接器对上。只改另两个文件会得到**链接错误（undefined reference）**，因为没人实例化 `LaunchTAdd<double, ...>`；必须在 kernel 文件里补一行显式实例化。这是四件套「隐式对齐」的第一个坑。

**练习 2**：`gen_data.py` 里 `golden[:row_valid, :col_valid] = (input1 + input2)[:row_valid, :col_valid]`，为什么无效区保持 0？

**答案**：PTO 指令只在有效区内有定义，`TSTORE` 只写回有效区（u2-l2、u3-l1）。golden 用全零初始化 + 有效区填充，模拟的正是「无效区不写、维持初始值」的硬件行为。若 kernel 侧正确设置了 tile 掩码，比对时无效区双方都是 0，通过；若越界写入则立刻暴露。

**练习 3**：CPU 用例和 NPU 用例的 `main.cpp` 能否共用同一份源码？

**答案**：能，而且仓库就是这么做的——`tests/npu/a2a3/src/st/testcase/tadd/main.cpp` 与 CPU 版结构逐行对应（TEST_F 命名同为 `TADDTest.case_float_64x64_64x64` 等）。差异被 `test_common.h` 吸收：[L23-L27](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/common/test_common.h#L23-L27) 只在非 `__CPU_SIM`、非 `__COSTMODEL` 时才引入 `acl/acl.h`，配合 CPU 桩库（u2-l4 的 `cpu_stub.hpp` 机制）把 `aclrtMalloc` 等 ACL 函数在宿主机上仿真实现。

### 4.2 run_st.py 与运行入口：定向运行与过滤机制

#### 4.2.1 概念说明

仓库有三层运行入口，各管一段：

| 入口 | 服务对象 | 特点 |
|---|---|---|
| `tests/script/run_st.py` | NPU 真机 / CAMS 仿真 | 单用例定向构建+造数+运行，支持 `-g` gtest 过滤 |
| `tests/run_cpu.py` | CPU 仿真 | CPU 路径总入口（u1-l3 讲过整体），同样支持单用例与 gtest 过滤 |
| `tests/run_st.sh` / `tests/run_cpu_tests.sh` | 一键全量 | 把上百条 `run_st.py -t xxx` 调用串起来 |

`run_st.py` 是本节主角：它是一个**编排器**，本身不含任何测试逻辑，职责是把「选目录 → 配环境 → 编译 → 造数 → 运行」五步串成一条命令。它的过滤机制有两层：**用例级**（`-t` 决定编译哪个目录，映射到 CMake 的 `TEST_CASE` 变量）与 **case 级**（`-g` 透传给二进制的 `--gtest_filter`，只跑套件里的部分用例）。通信用例还有第三层：**rank 级**（按 2/4/8 卡轮次自动构造 GTEST_FILTER）。

#### 4.2.2 核心流程

`run_st.py` 主流程（[main()](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/script/run_st.py#L270-L402)）：

```text
解析参数 (-r/-v/-t/-g/-d/-a/-w/-n)
  ↓
-v 映射内部 SOC_VERSION:  a3→Ascend910B1, a5→Ascend950PR_9599,
                          a6→dav_9201, kirin9030→Kirin9030 ...
  ↓
按 (是否 comm/, -v) 路由工作目录:
  comm/ 前缀 → tests/npu/<soc>/comm/st/
  否则      → tests/npu/<soc>/src/st/
  ↓
set_env_variables(sim 模式): 剥离 LD_LIBRARY_PATH 里的 /runtime/lib64，
  换成 ACL stub 库 + CAMS 仿真库，并 source CANN 的 setenv.bash
  ↓
build_project: rm -rf build → cmake -DRUN_MODE -DSOC_VERSION -DTEST_CASE → make -j
  ↓
run_gen_data: cp testcase/<t>/gen_data.py build/ → 在 build/ 里执行 python
  ↓
run_binary: cd build/bin → ./<t> [--gtest_filter=<g>]
  comm 用例: mpirun -n N 包裹；按 RANK_LEVELS=[2,4,8] 轮次执行，
  每轮用 "*4Ranks*" 之类的 GTEST_FILTER 挑出对应卡数的 case
```

CPU 侧的对应流程（`run_cpu.py`，细节见 u1-l3）在三个环节上同构：`perform_build` 用 `-DTEST_CASE=<t>` 过滤编译；`generate_golden` 把 `gen_data.py` 拷进 build 目录执行（带 `PYTHONPATH` 指向 ST 目录，让 `from utils import NumExt` 能找到）；`run_gtest_binary` 从 `build/bin/` 启动二进制——因为 main.cpp 里的数据路径都是 `../<套件.用例>/xxx.bin` 相对路径。

#### 4.2.3 源码精读

**(a) 参数与 SOC 映射**

- [run_st.py:L272-L284](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/script/run_st.py#L272-L284)：八个命令行参数。`-r`（sim/npu）与 `-v`（a3/a5/a6/kirin 系）必填；`-t` 是用例名；`-g` 可选 gtest 过滤；`-w` 跳过编译（需预先构建）；`-n` 限定通信测试最大 rank 数。
- [run_st.py:L287-L297](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/script/run_st.py#L287-L297)：`-v` 的短名到内部 SOC_VERSION 的映射表（默认 `Ascend910B1`）。这份映射与 u2-l4 讲过的 `arch_macro.hpp` 数字翻译（2201/3101/9201）是同一件事的两端：脚本端选目录、选链接库，头文件端选指令实现。

**(b) 目录路由**

- [run_st.py:L302-L306](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/script/run_st.py#L302-L306)：`-t comm/tget` 的 `comm/` 前缀被剥离并置 `is_comm` 标志——同一份脚本同时服务计算 ST 与通信 ST。
- [run_st.py:L314-L329](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/script/run_st.py#L314-L329)：按 SOC 短名 `chdir` 到对应 `tests/npu/<soc>/[comm/]st` 目录。注意 a3 走 `npu/a2a3`——「架构目录名」与「脚本短名」的对应关系（u1-l2 的平台矩阵）在这里落地。

**(c) 环境准备（sim 模式专属）**

- [run_st.py:L31-L66](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/script/run_st.py#L31-L66)：sim 模式要做两次 LD_LIBRARY_PATH 手术——先把宿主上真机版 `runtime` 库从路径里剔除（L33-L37，防止链接到真硬件驱动），再前置 ACL stub 库与 CAMS 仿真库（L43、L66）；L48-L61 还会 source CANN 自带的 `setenv.bash` 并把导出的环境变量吸收进当前进程。

**(d) 构建与造数**

- [run_st.py:L85-L126](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/script/run_st.py#L85-L126)：`build_project` 每次都**清空重建** build 目录（L88-L92），然后 [L102](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/script/run_st.py#L102) 用 `-DTEST_CASE=<t>` 只编一个用例——与 4.1.3(d) 的 `foreach` 过滤器首尾相接。`-d`/`-a` 分别追加 `DEBUG_MODE`/`AUTO_MODE`，对应 NPU CMake 里的调试打印与 Auto 模式开关（u9-l1）。
- [run_st.py:L129-L145](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/script/run_st.py#L129-L145)：`run_gen_data` 把用例的 `gen_data.py` 拷到 build 目录再原地执行——这样 `testcases/` 数据目录就生成在 build 里，与二进制的 `../` 相对路径约定对齐。

**(e) 运行与三层过滤**

- [run_st.py:L223-L267](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/script/run_st.py#L223-L267)：`run_binary` 进入 `build/bin/` 直接执行 `./<testcase>`；若传了 `-g`，则追加 `--gtest_filter=<g>`（L235-L237）。通信用例用 `mpirun -n N` 包裹（L246-L254），并自动探测 OpenMPI 补 `--allow-run-as-root`。
- [run_st.py:L348-L395](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/script/run_st.py#L348-L395)：通信用例不指定 `-g` 时按 [RANK_LEVELS = [2,4,8]](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/script/run_st.py#L173) 轮次执行，每轮由 [get_gtest_filter_for_nranks](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/script/run_st.py#L192-L200) 生成形如 `*-*4Ranks*:*8Ranks*`（2 卡轮）的过滤器——**rank 级过滤就是靠用例命名里的 `4Ranks`/`8Ranks` 后缀**实现的，与 4.1 的「目录名即套件名」一脉相承。CCU 用例（[L148-L150](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/script/run_st.py#L148-L150)）还要逐 TEST_F 隔离运行，[list_gtest_cases](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/script/run_st.py#L153-L170) 不执行二进制、直接正则解析 `main.cc` 源码里的 `TEST_F` 宏来枚举 case。

**(f) CPU 侧的同构机制**

- [run_cpu.py:L615-L646](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/run_cpu.py#L615-L646)：cmake 配置行，[L631](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/run_cpu.py#L631) 的 `-DTEST_CASE={args.testcase}` 与 run_st.py 完全同构；不带 `--testcase` 时用 `-UTEST_CASE` 清掉缓存值（L624），保证「跑全量」真的编全部。
- [run_cpu.py:L578-L612](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/run_cpu.py#L578-L612)：增量构建判定——读 CMakeCache 里的 `TEST_CASE` 值，与本次请求不一致就强制重新配置。这是「上次编了 tadd、这次要跑 tmul」场景的正确性保障。
- [run_cpu.py:L272-L282](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/run_cpu.py#L272-L282)：造数。注意 `PYTHONPATH` 被指到 ST 源目录（L274-L280），因为 `gen_data.py` 第一行就 `from utils import NumExt`。
- [run_cpu.py:L325-L339](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/run_cpu.py#L325-L339)：`run_gtest_binary` 从二进制所在目录启动（L336 的 `run_cwd`），`--gtest_filter` 在 L328-L329 追加——与 run_st.py 的 `-g` 行为一致。
- [all_cpu_tests.py:L127-L130](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/script/all_cpu_tests.py#L127-L130)：CPU 批量入口管理的两个工程（计算 ST + 通信 ST）；[L144-L163](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/script/all_cpu_tests.py#L144-L163) 用多进程池**并行跑完所有 gen_data.py**；[L166-L218](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/script/all_cpu_tests.py#L166-L218) 逐个二进制运行（每个 30 秒超时，从对应 `testcase/<name>` 目录启动），最后打印 `SUMMARY: TOTAL/PASSED/FAILED`。

#### 4.2.4 代码实践：定向运行单个用例

1. **实践目标**：体验「用例级 + case 级」两层过滤，理解一条命令背后发生了什么。
2. **操作步骤**（CPU 路径，无需 NPU）：
   ```bash
   # 只编译并运行 tadd 这一个用例（全部 case）
   python3 tests/run_cpu.py --testcase tadd --verbose
   # 进一步只跑 float 的一个 case
   python3 tests/run_cpu.py --testcase tadd --gtest_filter TADDTest.case_float_64x64_64x64 --verbose
   ```
   有 CANN ≥ 8.5 环境时再试 NPU/CAMS 路径：
   ```bash
   python3 tests/script/run_st.py -r sim -v a3 -t tadd
   python3 tests/script/run_st.py -r sim -v a3 -t tadd -g TADDTest.case_float_64x64_64x64
   ```
3. **需要观察的现象**：CPU 路径日志应依次出现 `[STEP] cmake configure`、`[STEP] gen_data: tadd`、gtest 的 `[ RUN ]`/`[ OK ]` 与最后的 PASS 汇总；第二次运行因 CMakeCache 的 TEST_CASE 未变会跳过重新编译，直接进入造数与运行，速度明显变快；`--gtest_filter` 生效时只看到一个 case 被执行。
4. **预期结果**：tadd 全部 case PASS。`run_st.py` 的 sim/npu 路径依赖 ASCEND_HOME_PATH、bisheng 编译器与 CAMS 仿真库，在无 CANN 的机器上会在环境检查或编译阶段失败——**待本地验证**（需 CANN 环境）。
5. 若 CPU 路径报 `unknown testcase`，说明该名字不在 `ALL_TESTCASES` 或目录缺失，可对照 4.1.3(d) 的注册表排查。

#### 4.2.5 小练习与答案

**练习 1**：`run_st.py -r sim` 与 `-r npu` 最终产出的二进制有什么不同？

**答案**：源码相同、CMake 的 `RUN_MODE` 不同。NPU 侧 `pto_vec_st` 的链接项按模式切换：`sim` 链 `runtime_camodel`（CAMS 仿真运行时），`npu` 链真机 `runtime`（见 [testcase/CMakeLists.txt:L35](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/npu/a2a3/src/st/testcase/CMakeLists.txt#L35)）。此外 sim 模式在运行前还要替换 LD_LIBRARY_PATH（4.2.3(c)），避免加载到真机驱动。

**练习 2**：为什么 `run_st.py` 每次构建都先 `rm -rf build`，而 `run_cpu.py` 却实现了增量构建？

**答案**：NPU 侧构建产物依赖 ASCEND_HOME_PATH 环境与模式切换（sim/npu 链不同库），残留的 CMakeCache 容易造成「上次 sim、这次 npu」的脏配置，干脆每次重建，简单可靠。CPU 侧环境稳定，`run_cpu.py` 通过读 CMakeCache 的 `TEST_CASE` 值检测配置漂移（[run_cpu.py:L578-L612](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/run_cpu.py#L578-L612)），仅在需要时才重新配置，为的是 131 个用例全量跑时的速度。

**练习 3**：通信用例如何做到「一次命令、多卡数自动轮跑」？

**答案**：靠命名约定 + 过滤器。[run_st.py:L173](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/script/run_st.py#L173) 定义 `RANK_LEVELS=[2,4,8]`，主循环对每个 rank 数构造 `*4Ranks*` 这样的 GTEST_FILTER 并 `mpirun -n N` 执行一次；用例作者把 4 卡场景的 TEST_F 命名带上 `4Ranks` 后缀，过滤器就能精确选中。tests/README.md 的 "How It Works" 一节描述的正是这一机制。

### 4.3 覆盖校验：用例命名与 run_st.sh 覆盖率

#### 4.3.1 概念说明

ST 体系里有三方信息各自独立维护，任何一方漂移都会让测试「静默失效」：

1. **用例目录**（`tests/npu/<soc>/src/st/testcase/<name>/`，含 `main.cpp`）；
2. **run_st.sh 的调用清单**（几百行 `python3 tests/script/run_st.py ... -v a3 -t <name>`）；
3. **`-g` 引用的 gtest case 名**（必须真的存在于 `main.cpp` 的 `TEST_F`）。

典型事故：新建了用例目录却忘了登记进 run_st.sh——全量门禁根本不会跑它，覆盖率出现暗洞；或者 run_st.sh 里写了 `-g TADDTest.case_float` 但 main.cpp 已改名——命令静默匹配不到任何 case，「通过」得毫无意义。两个静态校验脚本分别封堵这两个洞，都不需要硬件，纯文本扫描：

- `validate_op_coverage.py`：**方向是「目录 → 脚本」**，找「有用例、没登记」的遗漏。
- `validate_testcase_names.py`：**方向是「脚本 → 源码」**，找「脚本引用了不存在的 case 名」的死引用。

#### 4.3.2 核心流程

`validate_op_coverage.py` 的算法本质是一次**集合差**：

```text
对每个版本 v ∈ {a3, a5, kirin9030}:
    dirs    = { d | tests/npu/<v路径>/testcase/d/main.cpp 存在 }
    ops     = { op | run_st.sh 中出现 "-v <v> -t <op>" }
    missing = dirs - ops
missing 非空 → 打印清单并以退出码 1 结束
```

`validate_testcase_names.py` 反向校验：从 run_st.sh 与 run_pipeline.sh 中提取每个 `-v <v> -t <op> [-g <case>]` 引用，去对应 `main.cpp` 里解析 `TEST_F(套件, 用例)` 集合，验证引用存在。难点在 NPU 用例大量用宏生成 TEST_F，所以脚本内建了一个小型 C 预处理器（`MacroExpander`），支持 `#define`、`CONCAT(a,b)`、`##` 拼接与宏生成器。

两者合起来构成 CI 的静态门禁：退出码非 0 即失败。

#### 4.3.3 源码精读

- [validate_op_coverage.py:L32-L36](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/validate_op_coverage.py#L32-L36)：版本到用例目录的映射表。注意它只查 NPU 三套目录（CPU 用例由 `run_cpu.py` 按注册表全量发现，不存在「漏登记进 shell 清单」的问题——两种入口的覆盖策略不同）。
- [validate_op_coverage.py:L39-L65](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/validate_op_coverage.py#L39-L65)：`get_test_directories` 以「目录里有没有 `main.cpp`」为存在性判据，并处理符号链接（NPU 用例目录间常用 symlink 共享，先 `resolve()` 再判断）。
- [validate_op_coverage.py:L68-L82](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/validate_op_coverage.py#L68-L82)：`get_ops_in_script` 用正则 `-v\s+<版本>\s+-t\s+(\S+)` 从 run_st.sh 文本里抠出操作名。例如 run_st.sh 中真实的一行 [run_st.sh:L272](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/run_st.sh#L272)（`python3 tests/script/run_st.py $ARGS -w -v a3 -t tadd -g TADDTest.case_float_64x64_64x64`）会被解析为 a3 版本下的 op `tadd`。
- [validate_op_coverage.py:L85-L105](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/validate_op_coverage.py#L85-L105)：`check_missing_ops` 做集合差并按版本汇总。
- [validate_op_coverage.py:L108-L132](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/validate_op_coverage.py#L108-L132)：`main` 先 `chdir` 到项目根保证相对路径稳定，缺失时逐版本打印清单、返回 1。
- [validate_testcase_names.py:L11-L27](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/validate_testcase_names.py#L11-L27)：文档字符串写明它校验的对象（run_st.sh 与 run_pipeline.sh）与宏处理能力；[L48](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/validate_testcase_names.py#L48) 是待校脚本列表，[L87](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/validate_testcase_names.py#L87) 起的 `MacroExpander` 实现了 `#define`/`CONCAT`/`##`/宏生成器四类展开。

#### 4.3.4 代码实践：亲手触发一次覆盖校验

1. **实践目标**：观察两个校验脚本的输出形态，理解它们各自拦截哪类错误。
2. **操作步骤**（纯静态扫描，无需硬件）：
   ```bash
   python3 tests/validate_op_coverage.py; echo "exit=$?"
   python3 tests/validate_testcase_names.py; echo "exit=$?"
   ```
3. **需要观察的现象**：第一个脚本打印 `Checking for missing test operations in run_st.sh` 表头；若仓库当前所有 NPU 用例都已登记，会输出 `All operations are covered!` 且退出码 0。第二个脚本输出各脚本/版本的校验结论。可以做一个「思想实验」验证逻辑：在脑中给 `tests/npu/a2a3/src/st/testcase/` 建一个假目录 `fakeop/`（含空 main.cpp）但不登记 run_st.sh，重跑第一个脚本应报 `A3 (1 missing): - fakeop`。
4. **预期结果**：当前 HEAD 下两个脚本均应通过（退出码 0）；任何非 0 输出都意味着仓库自身的覆盖缺口。**待本地验证**（以实际输出为准）。
5. 请勿真的创建假目录提交——若想实验，在本地未提交状态试完即删。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `validate_op_coverage.py` 只校验 NPU 目录、不管 CPU 用例目录？

**答案**：两者的运行入口发现机制不同。NPU 全量靠 run_st.sh 里手工维护的几百行调用清单，可能漏登记；CPU 全量靠 `run_cpu.py` 直接枚举 CMake 注册表（`ALL_TESTCASES`）构建，「漏跑」只可能发生在「目录建了但没进注册表」，而那会导致用例根本不被编译，属于构建配置问题而非脚本覆盖问题。校验脚本对症下药，只查会静默失效的那条链。

**练习 2**：`get_ops_in_script` 的正则为什么用 `(\S+)` 而不是 `(\w+)`？

**答案**：用例名里可能出现普通字符类之外的符号。事实上本仓库用例名均为 `\w` 风格，用 `(\S+)` 是更宽松的写法，能吃到下一个空白符前的完整 token，避免因未来出现带连字符等符号的用例名而失配；代价是如果 `-t` 后面跟的是别的形态 token 也会被吞入，但脚本输入是受控的 shell 文件，风险可接受。

**练习 3**：如果 run_st.sh 里写了 `-g TADDTest.case_float_64x64` 而 main.cpp 里的 case 名是 `case_float_64x64_64x64_64x64_64x64`，会发生什么？哪个脚本能拦住？

**答案**：gtest_filter 是通配匹配，`case_float_64x64` 能匹配到以它为前缀的更长名字（`*` 后缀语义），所以大概率仍能跑通——这类「前缀碰巧匹配」不会报错。但若写成完全不沾边的名字，gtest 会报 `NO TESTS` 并以失败退出；`validate_testcase_names.py` 则在静态阶段就按精确/模式匹配规则校验引用存在性，把问题拦在跑测试之前。顺带一提，run_st.sh 中 a3 与 a5 的 tadd case 名不同（a5 版多了尺寸段，见 [run_st.sh:L886](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/run_st.sh#L886)），这正说明命名校验必须按版本分别做。

## 5. 综合实践

**任务：参照 tadd，从零新建一个 TMul 的 ST 用例并跑通。**

仓库里其实已经有官方 `tmul` 用例（注册于 [testcase/CMakeLists.txt:L105](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/CMakeLists.txt#L105)），所以我们的新用例取名 **`tmuldemo`**，做完后可与官方实现互相对照——这是最好的自评方式。

**步骤 1：创建四件套** `tests/cpu/st/testcase/tmuldemo/`

- `CMakeLists.txt`：照抄 [tadd 的 L11](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/CMakeLists.txt#L11)，改成 `pto_cpu_sim_st(tmuldemo)`。
- `tmuldemo_kernel.cpp`：复制 `tadd_kernel.cpp`，做三处替换——函数名 `runTAdd`→`runTMul`、指令 `TADD(dstTile, src0Tile, src1Tile)`→`TMUL(dstTile, src0Tile, src1Tile)`、导出名 `LaunchTAdd`→`LaunchTMul`。其余（TASSIGN 摆放、事件配对、TLOAD/TSTORE）原样保留。
- `main.cpp`：复制 tadd 版，把 `LaunchTAdd` 声明与调用改为 `LaunchTMul`，套件名 `TADDTest` 改为 `TMULDEMO_Test`（或任意名，但下一步的 gen_data 必须用同一个名字），TEST_F 的 case 名保持 `case_float_64x64_64x64` 等格式。
- `gen_data.py`：复制 tadd 版，把 golden 公式从 `input1 + input2` 改为 `input1 * input2`（[对照官方 tmul 的 gen_data.py:L29](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tmul/gen_data.py#L29)），`generate_case_name` 中的 `TADDTest` 前缀改成与 main.cpp 一致的套件名。随机输入范围 1~9 的整数可让 int16 乘法也不溢出（9×9=81 < 32767）。

**步骤 2：登记注册表**

在 [tests/cpu/st/testcase/CMakeLists.txt 的 ALL_TESTCASES](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/CMakeLists.txt#L39-L170) 列表里按字母序加一行 `tmuldemo`。

**步骤 3：运行验证（CPU 仿真）**

```bash
python3 tests/run_cpu.py --testcase tmuldemo --verbose
python3 tests/run_cpu.py --testcase tmuldemo --gtest_filter TMULDEMO_Test.case_float_64x64_64x64 --verbose
```

**预期结果**：全部 case PASS。然后做两个交叉检查：

1. 与官方 `tmul` 对拍：`diff` 你的 `tmuldemo_kernel.cpp` 与 [官方 tmul_kernel.cpp](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tmul/tmul_kernel.cpp#L14-L43)，应当几乎一致。
2. 故意把 golden 公式写错（例如仍用 `+`），重跑应 FAIL，且日志给出 max diff 与 err count——验证对拍真的在工作。

**进阶（可选，需 CANN 环境，待本地验证）**：把四件套同样复制到 `tests/npu/a2a3/src/st/testcase/tmuldemo/`（注册行改用 `pto_vec_st(tmuldemo)`），执行 `python3 tests/script/run_st.py -r sim -v a3 -t tmuldemo`，观察同一份用例在 CAMS 仿真路径下的构建与运行差异。

## 6. 本讲小结

- ST 的组织铁律是「**一目录一指令一可执行**」，四件套（kernel/main/gen_data/CMakeLists）职责固定，`pto_cpu_sim_st` 一行注册，`ALL_TESTCASES` 一处登记。
- 四件套靠三条**隐式约定**对齐：golden 目录名 = `TEST_F` 的 `套件.用例` 名；gen_data 的 case 参数表 = kernel 的显式模板实例化 = main 的 TEST_F 集合；数据文件以 `../<套件.用例>/` 相对路径从 `build/bin/` 访问。
- `run_st.py` 是 NPU/CAMS 路径的编排器：`-v` 选 SOC 目录、`-t` 经 CMake `TEST_CASE` 做编译期过滤、`-g` 透传 `--gtest_filter` 做 case 级过滤、通信用例再叠一层按 `4Ranks/8Ranks` 命名后缀的 rank 级轮次过滤；CPU 路径由 `run_cpu.py` 以同构机制覆盖。
- CPU 与 NPU 用例共享四件套结构与 `main.cpp` 写法，差异被构建函数（`pto_cpu_sim_st` vs `pto_vec_st/pto_cube_st`：单可执行 vs kernel 共享库 + host 可执行）与 `test_common.h`/CPU 桩吸收。
- `validate_op_coverage.py`（目录 → run_st.sh 的集合差）与 `validate_testcase_names.py`（脚本引用 → TEST_F 源码校验，含宏展开）封堵「漏登记」与「死引用」两类静默失效。
- CPU ST 验证指令语义，不验证流水线时序（事件为空桩）；时序正确性要靠 CAMS（`-r sim`）或真机（`-r npu`）。

## 7. 下一步学习建议

本讲你已掌握「一条指令如何被测试」。接下来：

1. **u10-l2（CPU 仿真器内幕）**：ST 的 CPU 路径之所以能跑，靠的是 `include/pto/cpu/NPUMemoryModel.hpp` 模拟的 GM/UB 存储层级与 `cpu_stub.hpp` 的 ACL 桩（本讲 4.1.5 练习 3 已埋下伏笔）——下一讲拆开这台仿真器。
2. **顺带读两个「非典型」用例**扩展视野：`testcase/tflashattn`（跨多条指令的组合算子级 ST）与 `tests/cpu/comm/st/`（通信指令的 MPI 化 CPU 测试），体会四件套约定的弹性。
3. **动手深化**：给综合实践的 `tmuldemo` 加一个 `int8` dtype 的 case，完整走一遍「gen_data 参数表 → kernel 显式实例化 → TEST_F」三处同步修改，把隐式约定变成肌肉记忆——这正是 u11-l1（新增一条指令）的预演。
