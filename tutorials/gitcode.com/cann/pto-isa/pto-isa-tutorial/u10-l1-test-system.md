# 测试体系：ST 用例结构与运行脚本

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 PTO 一条 ST（System Test）用例的「四件套」——`<op>_kernel.cpp`、`main.cpp`、`gen_data.py`、`CMakeLists.txt`——各自的职责与协作方式。
2. 理解 CPU 仿真树与 NPU 树共用同一套用例约定，只是注册函数（`pto_cpu_sim_st` vs `pto_vec_st`）与运行脚本（`run_cpu.py` vs `run_st.py`）不同。
3. 会用 `run_cpu.py` 的 `-t`/`-g` 参数定向构建、造数、运行单个用例；能读懂 `run_st.py` 在 NPU/sim 路径下的「构建 → 造 golden → 跑 gtest」三段式流程。
4. 理解两个守护脚本的作用：`validate_op_coverage.py` 防止「有用例没接进推荐套件」，`validate_testcase_names.py` 防止「脚本里引用的 case 名实际不存在或匹配 0 个测试」。
5. 独立为一条尚缺 CPU 用例的指令（本讲以 `tcolprod` 为例）新建四件套并跑通。

本讲是「二次开发」前的最后一块地基：u11-l1 新增一条指令时，ST 用例就是交付闭环的一部分。

## 2. 前置知识

阅读本讲前，你应当已经了解（对应前置讲义）：

- **CPU 仿真后端**（u1-l3、u2-l4）：`__CPU_SIM` 宏把同一份 kernel 源码路由到 CPU 仿真实现，`tests/run_cpu.py` 是 CPU 路径唯一入口。
- **指令的 CPU 仿真套路**（u3-l4）：一条指令是「公共 API 薄壳 → `*_IMPL` → 循环体」三层结构，CPU 仿真只求功能正确。
- **kernel 标准骨架**（u1-l4）：`GlobalTensor` 视图 → `TASSIGN` 绑定 Tile → `TLOAD` → 事件同步 → 计算 → 事件同步 → `TSTORE`。
- **Manual 模式缓冲摆放**（u3-l2）：`TASSIGN(tile, offset)` 里的 `0x0`/`0x4000`/`0x8000` 是手工规划的片上偏移，互不重叠是开发者责任。

补充两个本讲要用的新术语：

- **ST（System Test）**：在这里指「一条指令一个可执行程序」的端到端用例——C++ kernel 跑出 `output.bin`，与 Python（numpy）生成的 `golden.bin` 逐元素比对。它验证的是「指令 × 数据 × 布局」组合的功能行为，比单元测试更贴近真实算子用法。
- **golden（标杆数据）**：由 `gen_data.py` 用 numpy 独立计算的参考答案。C++ 实现与 Python 实现互为镜像，两边必须成对修改——这是 PTO 测试体系最重要的一条纪律。

## 3. 本讲源码地图

| 文件 / 目录 | 作用 |
|---|---|
| [tests/README.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/README.md) | 测试体系总入口文档：各脚本用法与目录布局 |
| [tests/cpu/st/testcase/tadd/](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd) | CPU 树标杆用例：四件套的最小完整样例 |
| [tests/cpu/st/testcase/CMakeLists.txt](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/CMakeLists.txt) | 定义 `pto_cpu_sim_st()` 注册函数与 `ALL_TESTCASES` 登记表 |
| [tests/cpu/st/utils.py](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/utils.py) | `NumExt`：gen_data 的 numpy 工具（含 bf16 位级转换） |
| [tests/common/test_common.h](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/common/test_common.h) | `ReadFile`/`WriteFile`/`ResultCmp` 等 main.cpp 公共设施 |
| [tests/script/run_st.py](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/script/run_st.py) | NPU/sim 模式 ST 的「构建+造数+运行」脚本 |
| [tests/run_cpu.py](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/run_cpu.py) | CPU 仿真 ST 的对应入口（增量构建、逐用例造数） |
| [tests/run_st.sh](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/run_st.sh) | 推荐套件编排：按平台逐用例调用 run_st.py |
| [tests/validate_op_coverage.py](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/validate_op_coverage.py) | 覆盖校验：用例目录 vs run_st.sh 引用 |
| [tests/validate_testcase_names.py](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/validate_testcase_names.py) | 命名校验：`-g Suite.Case` 引用 vs `TEST_F` 定义 |
| [include/pto/cpu/TColProd.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TColProd.hpp) | 综合实践的目标指令（有实现、缺 CPU 用例） |

## 4. 核心概念与源码讲解

本讲的三个最小模块：**用例结构**、**运行脚本**、**覆盖校验**。

### 4.1 用例结构：四件套与注册机制

#### 4.1.1 概念说明

PTO 的测试组织原则是**「一条指令一个目录、一个目录一个 gtest 可执行」**。CPU 树 [tests/cpu/st/testcase/](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase) 下约有 130 个用例目录（`tadd`、`tmul`、`tmatmul`……），目录名就是指令名的小写形式。

每个目录固定包含四个文件，缺一不可：

| 文件 | 职责 | 侧 |
|---|---|---|
| `<op>_kernel.cpp` | 用 PTO 指令写被测计算，导出 `Launch<OP>` 模板函数并显式实例化 | 设备侧（`AICORE`） |
| `main.cpp` | gtest 用例骨架：读输入 → 调 Launch → 写输出 → 与 golden 比对 | host 侧 |
| `gen_data.py` | 用 numpy 生成输入与 golden 二进制 | 造数 |
| `CMakeLists.txt` | 一行注册：调用上层定义的注册函数 | 构建 |

这套约定的价值在于**机械可复制**：对比 `tadd` 与 `tmul` 两个目录，从 kernel 到 main 几乎逐行同构，只有指令名与运算符不同。新用例不需要思考「怎么搭架子」，只需要填语义。

#### 4.1.2 核心流程

一次 ST 运行的完整数据流（CPU 路径，构建目录记为 `build/`）：

```text
python3 tests/run_cpu.py -t tadd
        │
        ├─ 1. cmake -DTEST_CASE=tadd → 只把 tadd 子目录加进构建
        ├─ 2. 编译出 build/bin/tadd（gtest 可执行，链接 gtest_main）
        ├─ 3. 把 tadd/gen_data.py 拷到 build/ 并执行
        │       └─ 在 build/TADDTest.case_float_64x64_64x64_64x64/
        │            写出 input1.bin / input2.bin / golden.bin
        └─ 4. 在 build/bin/ 下运行 ./tadd
                ├─ 读 ../TADDTest.case_.../input1.bin、input2.bin   ← "../" 回到 build/
                ├─ LaunchTAdd(...) → TLOAD/TADD/TSTORE → 写 output.bin
                └─ ReadFile golden.bin + output.bin → ResultCmp → EXPECT_TRUE
```

三个关键约定支撑这条链路：

1. **case 目录命名 = `<Suite>.<case>`**：gtest 套件名（如 `TADDTest`）+ case 名（如 `case_float_64x64_64x64_64x64`）。`gen_data.py` 建目录、`main.cpp` 寻数据，用的是同一个字符串。
2. **二进制在 `build/bin/` 运行，数据在 `build/<Suite.case>/`**：所以 main 里数据路径固定写成 `../<Suite.case>/...`。
3. **kernel 与 main 分属两个编译单元**：kernel 模板必须显式实例化，否则链接失败。

#### 4.1.3 源码精读

**（1）kernel 侧：tadd_kernel.cpp**

kernel 就是 u1-l4 学过的标准骨架，只是被模板参数化（类型 + 全局形状 + tile 形状），使一份代码服务多个 case：

- [tests/cpu/st/testcase/tadd/tadd_kernel.cpp:16-28](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/tadd_kernel.cpp#L16-L28)：`runTAdd` 模板——用 `Shape<1,1,1,R,C>`/`Stride` 描述 GM 视图，声明三个 `TileType::Vec` tile，再手工 `TASSIGN` 到 `0x0`/`0x4000`/`0x8000` 三个互不重叠的片上偏移（Manual 模式，回顾 u3-l2）。
- [tests/cpu/st/testcase/tadd/tadd_kernel.cpp:34-41](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/tadd_kernel.cpp#L34-L41)：`TLOAD` 两次 → `set_flag/wait_flag`（MTE2→V）→ `TADD` → `set_flag/wait_flag`（V→MTE3）→ `TSTORE`。CPU 仿真下事件是空桩（u2-l3），但写成真机正确的形式，同一份代码可上 NPU。
- [tests/cpu/st/testcase/tadd/tadd_kernel.cpp:54-62](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/tadd_kernel.cpp#L54-L62)：显式实例化清单——`LaunchTAdd<float,64,64,64,64>` 等 4~5 个组合，与 main.cpp 的 `TEST_F` 一一对应。`aclFloat16` 走 `half` 转接（L48-51），bf16 实例化由 `CPU_SIM_BFLOAT_ENABLED` 宏门控。

**（2）host 侧：main.cpp**

- [tests/cpu/st/testcase/tadd/main.cpp:27-34](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/main.cpp#L27-L34)：`GetGoldenDir()`——从 gtest 运行时取「当前套件名.case 名」，拼出 `../TADDTest.case_...`。**这就是 gen_data 建的目录名与 gtest 命名自动对齐的机关**。
- [tests/cpu/st/testcase/tadd/main.cpp:44-68](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/main.cpp#L44-L68)：测试主体——`aclInit`/`aclrtSetDevice`/`aclrtMalloc*`（CPU 仿真下全部是 [cpu_stub.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/cpu_stub.hpp) 提供的宿主机桩实现，「device 指针」就是普通内存），`ReadFile` 两个输入，H2D 拷贝，调 `LaunchTAdd`，同步，D2H 拷回并 `WriteFile` 写 `output.bin`。
- [tests/cpu/st/testcase/tadd/main.cpp:83-90](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/main.cpp#L83-L90)：读 `golden.bin` 与 `output.bin`，`ResultCmp<T>(golden, devFinal, 0.001f)` 容差比对后 `EXPECT_TRUE`。
- [tests/cpu/st/testcase/tadd/main.cpp:93-96](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/main.cpp#L93-L96)：`TEST_F(TADDTest, case_float_64x64_64x64_64x64)` 等 4 个 case，命名格式 `case_<dtype>_<全局RxC>_<tileRxC>_<有效RxC>`。

比对与读写的公共设施在 [tests/common/test_common.h:64-128](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/common/test_common.h#L64-L128)（`ReadFile`/`WriteFile`，按 `__CPU_SIM`/`__COSTMODEL` 决定是否包含 `acl/acl.h`）；容差比较在 [tests/common/test_common.h:231-253](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/common/test_common.h#L231-L253)——判错条件是「绝对差 > eps **且** 相对差 > eps」，同时统计「期望非零、实际为零」的退化计数。

**（3）造数：gen_data.py**

- [tests/cpu/st/testcase/tadd/gen_data.py:21-38](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/gen_data.py#L21-L38)：golden 公式——`input1/input2` 取 `randint(1,10)` 后 astype，golden 在**有效区**内做加法、有效区外保持 0（与 TSTORE 只写回有效区的语义对齐，u3-l1）。
- [tests/cpu/st/testcase/tadd/gen_data.py:53-64](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/gen_data.py#L53-L64)：`generate_case_name` 用 `NumExt.get_short_type_name` 拼出 `TADDTest.case_float_64x64_...`——**与 main.cpp 的 TEST_F 名逐字符一致**，这是 gen 目录与 gtest 寻址对齐的第二处机关。
- [tests/cpu/st/testcase/tadd/gen_data.py:76-92](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/gen_data.py#L76-L92)：参数表驱动——每个 `TAddParams` 生成一个 case 目录并 `os.chdir` 进去写 bin。注意目录是按**当前工作目录**（即 build/）创建的，这正是数据落在 `build/` 根下的原因。

numpy 工具 `NumExt` 在 [tests/cpu/st/utils.py:17-48](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/utils.py#L17-L48)：`astype/zeros/write_array` 对 bf16 做了位级模拟（numpy 原生无 bf16，L50-61 手写「截断+舍入」转换），dtype 简名映射在 [tests/cpu/st/utils.py:63-76](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/utils.py#L63-L76)。`from utils import NumExt` 能生效，靠的是运行脚本把 `PYTHONPATH` 指到 `tests/cpu/st`（见 4.2.3）。

**（4）注册：一行 CMakeLists 与登记表**

- [tests/cpu/st/testcase/tadd/CMakeLists.txt:10](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/CMakeLists.txt#L10)：整个文件只有一行有效内容 `pto_cpu_sim_st(tadd)`。
- [tests/cpu/st/testcase/CMakeLists.txt:11-35](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/CMakeLists.txt#L11-L35)：`pto_cpu_sim_st(NAME)` 函数——自动收集 `main.cpp` + 「存在则加入」的 `<NAME>_kernel.cpp`，配好 include 路径（`include/`、stubs、common），链接 gtest_main 与线程库。**注意文件名约定：kernel 文件必须叫 `<目录名>_kernel.cpp`，否则不会被编进目标**。
- [tests/cpu/st/testcase/CMakeLists.txt:39-46](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/CMakeLists.txt#L39-L46)：`ALL_TESTCASES` 登记表（约 130 项，按字母序）。
- [tests/cpu/st/testcase/CMakeLists.txt:172-176](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/CMakeLists.txt#L172-L176)：`foreach` + `TEST_CASE` 过滤——不定义 `TEST_CASE` 则全部构建；定义了则只 `add_subdirectory` 匹配的那一个。这就是 `-t tadd` 只编译一个可执行的机制源头。

顶层 [tests/cpu/st/CMakeLists.txt:31](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/CMakeLists.txt#L31) 统一定义 `add_definitions(-D__CPU_SIM)`，把整棵测试树切到 CPU 仿真后端（u2-l4 的编译期路由）；GTest 优先找系统安装、找不到则 FetchContent 拉取 v1.14.0（[tests/cpu/st/CMakeLists.txt:59-103](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/CMakeLists.txt#L59-L103)）。

NPU 树是同构的：[tests/npu/a2a3/src/st/testcase/tadd/CMakeLists.txt:10](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/npu/a2a3/src/st/testcase/tadd/CMakeLists.txt#L10) 注册函数换成 `pto_vec_st(tadd)`（Cube 类指令用 `pto_cube_st`，混合用 `pto_mix_st`），四件套布局不变。

#### 4.1.4 代码实践：用 diff 看「模板有多机械」

1. **实践目标**：直观确认「新增用例 = 复制四件套 + 改语义」，并定位所有需要改的行。
2. **操作步骤**：
   ```bash
   cd <仓库根目录>
   diff -u tests/cpu/st/testcase/tadd/tadd_kernel.cpp tests/cpu/st/testcase/tmul/tmul_kernel.cpp
   diff -u tests/cpu/st/testcase/tadd/main.cpp        tests/cpu/st/testcase/tmul/main.cpp
   diff -u tests/cpu/st/testcase/tadd/gen_data.py     tests/cpu/st/testcase/tmul/gen_data.py
   ```
3. **需要观察的现象**：kernel 侧差异集中在 [tmul_kernel.cpp:38](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tmul/tmul_kernel.cpp#L38) 的 `TMUL(dstTile, src0Tile, src1Tile)` 一行与函数名；main 侧差异在 `TEST_F(TMULTest, ...)` 命名（[tmul/main.cpp:93-96](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tmul/main.cpp#L93-L96)）；gen_data 侧差异在 golden 公式 `input1 * input2` 与套件名前缀。
4. **预期结果**：三个 diff 的有效改动行数总和大约只有十几行；骨架（TASSIGN/TLOAD/事件/TSTORE、acl 初始化、文件读写）完全零改动。具体行数待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 [tadd_kernel.cpp:54-58](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/tadd_kernel.cpp#L54-L58) 末尾要写那几行 `template void LaunchTAdd<...>`？删掉会怎样？

> 答案：kernel 与 main.cpp 是两个编译单元，main.cpp 里只有模板声明（[main.cpp:36-37](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/main.cpp#L36-L37)）。模板不被使用式实例化就不会生成定义，链接期报 undefined reference to `LaunchTAdd<float, 64,64,64,64>`。显式实例化把「支持哪些 dtype × 形状组合」固化为一份清单，gen_data 的参数表必须与它同步。

**练习 2**：新建了用例目录却忘了把名字加进 `ALL_TESTCASES`，会发生什么？

> 答案：`foreach` 不会 `add_subdirectory` 它，CMake 目标根本不生成；`run_cpu.py -t <name>` 会在 `execute_tests` 阶段报 `unknown testcase`（可执行不在 `build/bin` 里）。反过来，只登记、不建目录，则 CMake 配置期直接报「子目录不存在」。

**练习 3**：`GetGoldenDir()` 里的 `"../"` 前缀为什么刚好能命中数据目录？

> 答案：gtest 可执行放在并运行于 `build/bin/`（[tests/run_cpu.py:325-339](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/run_cpu.py#L325-L339) 以可执行所在目录为 cwd），而 gen_data 在 `build/` 下建 `TADDTest.case_*` 目录；`build/bin/../TADDTest.case_*` 即 `build/TADDTest.case_*`。

### 4.2 运行脚本：run_st.py（NPU/sim）与 run_cpu.py（CPU 仿真）

#### 4.2.1 概念说明

PTO 有两条 ST 运行路径、两个入口脚本，但流程骨架相同：**路由到对应测试树 → 构建（可用 `-DTEST_CASE` 只构建一个）→ 拷贝 gen_data.py 到 build 并执行 → 在 build/bin 下跑 gtest（可带 `--gtest_filter`）**。

| | `tests/script/run_st.py` | `tests/run_cpu.py` |
|---|---|---|
| 目标树 | `tests/npu/<soc>/src/st`（或 `comm/st`） | `tests/cpu/st` |
| 运行模式 | `-r sim`（CAMS 仿真器）/ `-r npu`（真机） | 纯 CPU 仿真 |
| 环境要求 | Linux + CANN（`ASCEND_HOME_PATH`），sim 还需 simulator 组件 | 仅 C++20 编译器 + cmake + numpy |
| 单用例 | `-t tadd` | `-t tadd` |
| case 过滤 | `-g TADDTest.case_*` | `-g TADDTest.case_*` |

**不要混淆**：CPU 仿真 ST 不能用 `run_st.py` 跑——它按 `-v` 平台号切换目录时只会落到 `tests/npu/...`，且 `-r sim` 依赖 CANN 安装。

#### 4.2.2 核心流程

`run_st.py` 主流程（NPU 路径）：

```text
解析参数 (-r/-v/-t/-g/-w/-n)
  → 按 -v 选目标目录（a3→tests/npu/a2a3/src/st，comm/ 前缀→.../comm/st）
  → set_env_variables：sim 模式改 LD_LIBRARY_PATH（剔除真机 runtime、注入 stub 与 camodel 库）
  → build_project：cmake -DTEST_CASE=<t> .. && make -j
  → run_gen_data：cp testcase/<t>/gen_data.py build/ && (cd build && python3 gen_data.py)
  → run_binary：cd build/bin && ./<t> [--gtest_filter=<g>]
      └─ comm 用例：mpirun -n <ranks> 包裹，按 2/4/8 rank 轮次配 *4Ranks/*8Ranks 过滤器
```

`run_cpu.py` 主流程（CPU 路径，更讲究增量）：

```text
检测编译器（clang++≥15 或 g++≥13）
  → determine_need_build：比较「请求的 -t」与 CMakeCache 里的 TEST_CASE；
     不一致或二进制缺失 → 重新 cmake（-DTEST_CASE=<t>）
  → 对每个选中用例：拷 gen_data.py 到 build/（PYTHONPATH 指到 tests/cpu/st）
     → 运行 ./build/bin/<t>（cwd=build/bin）
  → 打印 Target/Status/Time 汇总表
```

#### 4.2.3 源码精读

**（1）run_st.py：参数、路由与三段式**

- [tests/script/run_st.py:270-286](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/script/run_st.py#L270-L286)：argparse 定义——`-r`（sim/npu）、`-v`（a3/a5/a6/kirin 系列）、`-t`（用例名，`comm/` 前缀表示通信用例）、`-g`（gtest 过滤）、`-w`（跳过构建）、`-n`（comm 最大 rank 数）。
- [tests/script/run_st.py:308-332](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/script/run_st.py#L308-L332)：平台 → 目标目录路由表；`-v a3` 映射内部 SOC 版本 `Ascend910B1`、`a5` 映射 `Ascend950PR_9599`（L287-297）。
- [tests/script/run_st.py:85-126](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/script/run_st.py#L85-L126)：`build_project`——每次**清空重建** build 目录，cmake 传入 `-DRUN_MODE`、`-DSOC_VERSION`、`-DTEST_CASE`（L102），即 4.1.3 里 foreach 过滤的源头。
- [tests/script/run_st.py:129-145](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/script/run_st.py#L129-L145)：`run_gen_data`——把用例目录里的 `gen_data.py` 拷到 `build/` 再执行，所以相对路径产出的 `input*.bin/golden.bin` 落在 `build/` 与 `build/<Suite.case>/` 下。
- [tests/script/run_st.py:223-261](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/script/run_st.py#L223-L261)：`run_binary`——`cd build/bin`，`./<testcase>`，`-g` 非空时追加 `--gtest_filter=`（L236-237）；comm 用例再包一层 `mpirun -n <ranks>` 并定位 `libmpi.so`。
- [tests/script/run_st.py:192-200](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/script/run_st.py#L192-L200)：通信用例的 rank 约定——case 名带 `4Ranks/8Ranks` 后缀表示参与卡数，脚本按 2/4/8 轮次拼 gtest 过滤器。

**（2）run_cpu.py：CPU 路径的定向运行**

- [tests/run_cpu.py:440-456](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/run_cpu.py#L440-L456)：`-t/--testcase`（单个用例）、`-g/--gtest_filter`（case 过滤）、`--clean/--rebuild/--no-build/--no-gen` 等增量控制。
- [tests/run_cpu.py:615-646](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/run_cpu.py#L615-L646)：`perform_build`——cmake 配置参数里 `-DTEST_CASE=<t>`（L631）与 CPU 树 foreach 打通；不传 `-t` 时用 `-UTEST_CASE` 清掉旧缓存值。
- [tests/run_cpu.py:578-612](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/run_cpu.py#L578-L612)：`determine_need_build`——读 CMakeCache 里的 `TEST_CASE` 与请求值比对；若上次构建只编了单用例而这次要「跑全部」，强制重配（L585-589 注释写明动机：防止把旧子集当成全部而漏跑）。`parse_expected_testcases`（[L555-575](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/run_cpu.py#L555-L575)）还会解析 `ALL_TESTCASES` 登记表，与 `build/bin` 里实际二进制求差集，缺一个就触发重建。
- [tests/run_cpu.py:677-699](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/run_cpu.py#L677-L699)：逐用例造数——找到 `testcase/<t>/gen_data.py` 后调用 `generate_golden`。
- [tests/run_cpu.py:272-282](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/run_cpu.py#L272-L282)：`generate_golden`——把 gen 脚本拷到 build 目录执行，并把 `PYTHONPATH` 设为脚本路径上溯三级（= `tests/cpu/st`），使 `from utils import NumExt` 可导入。
- [tests/run_cpu.py:325-339](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/run_cpu.py#L325-L339)：`run_gtest_binary`——以可执行所在目录（`build/bin`）为 cwd 运行，可带 `--gtest_filter` 与 `--gtest_output=xml`。

**（3）run_st.sh：推荐套件的编排方式**

[tests/run_st.sh:136-165](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/run_st.sh#L136-L165) 展示了 a3 平台的编排模式：先 `build_st.py -v a3 -t all` 一次构建，然后对每个推荐用例调 `run_st.py -w -v a3 -t <op> -g <Suite.Case>`（`-w` 复用已编译二进制），并用 `ST_PART` 把用例分片便于并行。**每条 `-v <平台> -t <用例>` 引用都会被 4.3 的覆盖校验脚本盘点**。

#### 4.2.4 代码实践：定向运行单个用例

1. **实践目标**：跑通 CPU 仿真下「只构建 tadd → 只跑一个 case」，并看懂 build 目录的产物布局。
2. **操作步骤**：
   ```bash
   cd <仓库根目录>
   # 只构建并运行 tadd 用例
   python3 tests/run_cpu.py -t tadd --verbose
   # 只跑其中一个 case
   python3 tests/run_cpu.py -t tadd -g TADDTest.case_half_16x256_16x256_16x256 --no-build
   # 观察产物
   ls tests/cpu/st/build/bin/
   ls tests/cpu/st/build/ | head
   ls tests/cpu/st/build/TADDTest.case_float_64x64_64x64_64x64/
   ```
3. **需要观察的现象**：
   - 日志出现 `[STEP] cmake configure`（带 `-DTEST_CASE=tadd`）→ `[STEP] cmake build` → `[STEP] gen_data: tadd` → `[PASS] tadd`；
   - `build/bin/` 下只有 `tadd` 一个（或少数）可执行；
   - `build/TADDTest.case_float_64x64_64x64_64x64/` 下有 `input1.bin`、`input2.bin`、`golden.bin`、`output.bin` 四个文件；
   - 结尾打印 `Target | Status | Time` 汇总表。
4. **预期结果**：tadd 全部 case PASSED（float/int32/int16/half 四个；bf16 仅在 `--enable-bf16` 时存在）。`--no-build` 复用二进制重跑单个 case 应明显更快。具体耗时待本地验证。
   若你手头有 CANN 环境（Linux + 昇腾硬件或 simulator），可对照执行 NPU 路径：`python3 tests/script/run_st.py -r sim -v a3 -t tadd -g TADDTest.case_float_64x64_64x64`（待本地验证，本环境无 CANN）。

#### 4.2.5 小练习与答案

**练习 1**：`-t` 和 `-g` 都能缩小运行范围，本质区别是什么？

> 答案：`-t` 作用于**构建期**——映射为 CMake 变量 `TEST_CASE`，决定 `add_subdirectory` 哪个用例目录、生成哪个可执行；`-g` 作用于**运行期**——透传为 gtest 的 `--gtest_filter`，在一个可执行内部的多个 `TEST_F` 之间挑选。`-t tmul -g TADDTest.*` 会因为 tmul 二进制里没有 TADDTest 套件而匹配 0 个测试。

**练习 2**：`run_st.py` 的 `-w`（`--without-build`）为什么在 `run_st.sh` 里被大量使用？

> 答案：`run_st.sh` 先用 `build_st.py -v a3 -t all` 把全部用例一次性编译好，之后逐用例调 `run_st.py -w ...` 直接复用二进制，免去「每个用例都清空重建 build」的开销（`build_project` 每次会 `shutil.rmtree(build)`，见 [run_st.py:88-91](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/script/run_st.py#L88-L91)）。

**练习 3**：CPU 路径下，若上次执行过 `run_cpu.py -t tadd`，这次直接 `run_cpu.py`（不带 -t），脚本如何避免漏跑？

> 答案：`determine_need_build` 读出 CMakeCache 中残留的 `TEST_CASE=tadd`，与「跑全部」的意图不一致即判定 `config_mismatch`，强制重新配置为全量构建；同时用 `parse_expected_testcases` 对照 `ALL_TESTCASES` 与已有二进制，缺任何一个也会触发重建（[run_cpu.py:578-612](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/run_cpu.py#L578-L612)）。

### 4.3 覆盖校验：两个守护脚本

#### 4.3.1 概念说明

测试体系最怕两种静默失效：**「用例存在但没被任何入口执行」**与**「脚本里写的 case 名拼错了，gtest 匹配 0 个测试照样退出 0」**。PTO 用两个静态校验脚本在 CI 里堵这两类洞：

| 脚本 | 防的问题 | 比对双方 |
|---|---|---|
| `validate_op_coverage.py` | 用例目录没人接进推荐套件 | `tests/npu/<v>/src/st/testcase/` 下含 `main.cpp` 的目录 **−** `run_st.sh` 中 `-v <v> -t <op>` 引用 |
| `validate_testcase_names.py` | `-g Suite.Case` 引用不存在 / 匹配 0 测试 | `run_st.sh`、`run_pipeline.sh` 中 `-g` 引用 **vs** 对应 `main.cpp` 里的 `TEST_F` 定义（含宏展开）与 gtest 运行日志 |

两者都面向 **NPU 树**（CPU 树的对应保障是 `ALL_TESTCASES` 登记表 + `run_cpu.py` 的差集检查，见 4.2.3）。

#### 4.3.2 核心流程

`validate_op_coverage.py`：

```text
for version in {a3, a5, kirin9030}:
    test_dirs   = {d : tests/npu/<version>/src/st/testcase/d 含 main.cpp（跟随符号链接）}
    ops_in_sh   = {op : run_st.sh 中出现 "-v <version> -t <op>" 的 op}
    missing     = test_dirs − ops_in_sh
有 missing → 打印清单，exit 1；否则 exit 0
```

`validate_testcase_names.py`：

```text
for script in {run_st.sh, run_pipeline.sh}:
    for 每条 "-v <ver> -t <op> -g <Suite.Case>" 引用:
        cases = 用迷你 C 预处理器（#define / CONCAT / ## 拼接 / 宏生成器）
                从 tests/npu/<ver>/src/st/testcase/<op>/main.cpp 抽出的全部 TEST_F 全名
        <Suite.Case> ∉ cases → 记 issue
另有 --check-run <log> 模式：解析 gtest 日志，任何一次 filter 匹配 0 个测试即失败
```

#### 4.3.3 源码精读

- [tests/validate_op_coverage.py:32-36](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/validate_op_coverage.py#L32-L36)：三个平台的 testcase 路径映射。
- [tests/validate_op_coverage.py:39-65](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/validate_op_coverage.py#L39-L65)：`get_test_directories`——以「目录里有 `main.cpp`」为用例判据；符号链接先 `resolve()` 再查（kirin 系列平台间共享代码用 symlink 指向同一目录）。
- [tests/validate_op_coverage.py:68-82](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/validate_op_coverage.py#L68-L82)：`get_ops_in_script`——正则 `-v\s+<version>\s+-t\s+(\S+)` 抓取脚本中的用例引用。
- [tests/validate_op_coverage.py:97-105](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/validate_op_coverage.py#L97-L105)：做集合差 `test_dirs - ops_in_script` 得到漏网用例。
- [tests/validate_testcase_names.py:87-97](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/validate_testcase_names.py#L87-L97)：`MacroExpander`——NPU 的 `main.cpp` 常用宏批量生成 case（如 `CONCAT(CASENAME, Test)`、`case_##type##_...`、`GENERATE_TCVT_TESTS(...)`），校验器内置了一个小型预处理器来还原真实 `TEST_F` 名。
- [tests/validate_testcase_names.py:242](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/validate_testcase_names.py#L242) 与 [L308-384](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/validate_testcase_names.py#L308-L384)：`extract_test_cases_from_main` 与 `check_script`——静态比对的主体；引用不存在时输出 issue 并列出该 main.cpp 的全部可用 case 名，方便改正。
- [tests/validate_testcase_names.py:418-431](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/validate_testcase_names.py#L418-L431)：**运行期校验的设计动机**（脚本内注释）：gtest 对匹配 0 个测试的 filter 仍退出 0 并打印 `[  PASSED  ] 0 tests`——静态检查抓不到这种情况，因此提供 `--check-run <gtest日志>` 模式，凡某次 filter 实际跑了 0 个测试即判失败（[L434-459](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/validate_testcase_names.py#L434-L459) 解析 `Running N tests` 行）。

#### 4.3.4 代码实践：亲手触发一次校验失败

1. **实践目标**：看到两个校验脚本「抓漏」的真实输出，建立对 CI 防线的直觉。
2. **操作步骤**（在本地仓库副本上，结束记得还原）：
   ```bash
   # 基线：先看通过时的输出
   python3 tests/validate_op_coverage.py; echo "exit=$?"
   python3 tests/validate_testcase_names.py; echo "exit=$?"
   # 制造一个漏网用例：临时注释掉 run_st.sh 中一条 a3 引用
   cp tests/run_st.sh /tmp/run_st.sh.bak
   sed -i 's#-v a3 -t tadd -g#-v a3 -t taddX -g#' tests/run_st.sh   # 或手工注释 L272 一行
   python3 tests/validate_op_coverage.py; echo "exit=$?"
   python3 tests/validate_testcase_names.py; echo "exit=$?"
   mv /tmp/run_st.sh.bak tests/run_st.sh   # 还原
   ```
3. **需要观察的现象**：
   - 基线两次运行均打印成功信息、`exit=0`（待本地验证，取决于当前仓库状态）；
   - 改动后：`validate_op_coverage.py` 在 `A3` 段列出 `- tadd`（目录存在但引用没了），`exit=1`；`validate_testcase_names.py` 报 `taddX` 的 main.cpp 不存在或 case 失配，`exit=1`。
4. **预期结果**：还原 `run_st.sh` 后两个脚本回到 `exit=0`。两个脚本均为纯静态解析（不碰设备、不编译），在任何有 Python3 的机器上都能跑。

#### 4.3.5 小练习与答案

**练习 1**：为什么「gtest filter 匹配 0 个测试」比「用例失败」更危险？

> 答案：失败会亮红，而 0 匹配的 gtest 以退出码 0 结束、打印 `PASSED 0 tests`，CI 会当成通过——用例名拼错、套件改名、宏展开变化都会造成这种「假覆盖」。所以 `validate_testcase_names.py` 除了静态比对名字，还提供 `--check-run` 日志模式专门抓 `Running 0 tests`（[L420-431](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/validate_testcase_names.py#L420-L431)）。

**练习 2**：`validate_op_coverage.py` 判断「这是一个用例」的判据是什么？为什么要跟随符号链接？

> 答案：判据是**目录下存在 `main.cpp`**（[L55-63](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/validate_op_coverage.py#L55-L63)），而非目录名。跟随 symlink 是因为 kirin 等平台的用例目录是指向共享实现的链接，直接查链接自身路径下的 `main.cpp` 会落空，必须 `resolve()` 到真实目录再判。

**练习 3**：你为 a3 新增了一个 NPU 用例目录 `tfoo`，忘了在 `run_st.sh` 加引用，CI 里哪个环节会拦住你？

> 答案：`validate_op_coverage.py` 的集合差 `test_dirs − ops_in_script` 会把 `tfoo` 列进 `A3 missing` 清单并以 `exit=1` 失败（[L97-105](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/validate_op_coverage.py#L97-L105)）。若你加了引用但 `-g` 名字写错，则由 `validate_testcase_names.py` 拦截。

## 5. 综合实践

**任务**：为 `TCOLPROD`（列乘积规约）新建 CPU ST 用例并跑通。

先交代一个事实核查结论：大纲原始任务是「为 TMul 新建用例」，但仓库中 `tmul` 用例**已经存在**（[tests/cpu/st/testcase/tmul/tmul_kernel.cpp:38](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tmul/tmul_kernel.cpp#L38) 已有 `TMUL` 调用），同名新建会与 `ALL_TESTCASES` 登记表冲突。因此本实践改做**真实存在的覆盖缺口**：`TColProd` 在 [include/pto/cpu/TColProd.hpp:17-34](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TColProd.hpp#L17-L34) 有 CPU 实现、在 NPU 树 [tests/npu/a2a3/src/st/testcase/tcolprod/](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/npu/a2a3/src/st/testcase/tcolprod) 有用例，但 CPU 树没有对应目录。若你单纯想练 TMul，可把 tadd 四件套复制为自练习目录（如 `tmul_practice/`，kernel 文件名须为 `tmul_practice_kernel.cpp`）本地跑通后删除，勿提交。

### 5.1 步骤一：建目录写四件套

创建 `tests/cpu/st/testcase/tcolprod/`，放入以下四个文件。

**（1）`tcolprod_kernel.cpp`**（示例代码，仿照 [tadd_kernel.cpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/tadd_kernel.cpp) 改写；TCOLPROD 的 CPU 实现见 [TColProd.hpp:20-27](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TColProd.hpp#L20-L27)：沿列累乘、结果写在 dst 的第 0 行）：

```cpp
// 示例代码：仅保留关键行，版权头与 include 同 tadd_kernel.cpp
template <typename T, int kRows_, int kCols_>
AICORE void runTColProd(__gm__ T __out__* out, __gm__ T __in__* src)
{
    using Shape5 = Shape<1, 1, 1, kRows_, kCols_>;
    using Stride5 = Stride<1, 1, 1, kCols_, 1>;
    using OutShape5 = Shape<1, 1, 1, 1, kCols_>;      // 输出是 1 行
    using GlobalIn = GlobalTensor<T, Shape5, Stride5>;
    using GlobalOut = GlobalTensor<T, OutShape5, Stride5>;
    using TileIn = Tile<TileType::Vec, T, kRows_, kCols_, BLayout::RowMajor, -1, -1>;
    using TileOut = Tile<TileType::Vec, T, 1, kCols_, BLayout::RowMajor, -1, -1>;

    TileIn srcTile(kRows_, kCols_);                   // 构造参数 = 动态有效区
    TileOut dstTile(1, kCols_);
    TASSIGN(srcTile, 0x0);                            // 输入在前 4KB
    TASSIGN(dstTile, 0x4000);                         // 输出偏移不重叠（u3-l2 纪律）

    GlobalIn srcGlobal(src);
    GlobalOut dstGlobal(out);
    TLOAD(srcTile, srcGlobal);
    set_flag(PIPE_MTE2, PIPE_V, EVENT_ID0);
    wait_flag(PIPE_MTE2, PIPE_V, EVENT_ID0);
    TCOLPROD(dstTile, srcTile);                       // 被测指令
    set_flag(PIPE_V, PIPE_MTE3, EVENT_ID0);
    wait_flag(PIPE_V, PIPE_MTE3, EVENT_ID0);
    TSTORE(dstGlobal, dstTile);
}

template <typename T, int kRows_, int kCols_>
void LaunchTColProd(T* out, T* src, void* stream)
{
    runTColProd<T, kRows_, kCols_>(out, src);
}
// 显式实例化清单——与 main.cpp 的 TEST_F 一一对应
template void LaunchTColProd<float, 32, 32>(float* out, float* src, void* stream);
```

**（2）`main.cpp`**（示例代码，骨架同 [tadd/main.cpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/main.cpp)，仅列差异）：

```cpp
// 示例代码
class TCOLPRODTest : public testing::Test { /* 同 TADDTest */ };

template <typename T, int kRows_, int kCols_>
void LaunchTColProd(T* out, T* src, void* stream);    // 声明

template <typename T, int kRows_, int kCols_>
void test_tcolprod()
{
    size_t inSize = kRows_ * kCols_ * sizeof(T);      // 注意：输入输出尺寸不同
    size_t outSize = kCols_ * sizeof(T);
    // aclInit/Malloc/ReadFile(GetGoldenDir()+"/input.bin", inSize, ...) 同 tadd
    LaunchTColProd<T, kRows_, kCols_>(dstDevice, srcDevice, stream);
    // ... 写 output.bin（outSize 字节）、读 golden.bin（outSize 字节）
    bool ret = ResultCmp<T>(golden, devFinal, 0.001f);
    EXPECT_TRUE(ret);
}

TEST_F(TCOLPRODTest, case_float_32x32) { test_tcolprod<float, 32, 32>(); }
```

**（3）`gen_data.py`**（示例代码，仿 [tadd/gen_data.py](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/gen_data.py)；golden 公式与 NPU 版 [tcolprod/gen_data.py:25-30](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/npu/a2a3/src/st/testcase/tcolprod/gen_data.py#L25-L30) 的 `np.prod(axis=0)` 对齐）：

```python
# 示例代码
import numpy as np
from utils import NumExt

np.random.seed(19)

def gen_golden_data(row, col):
    dtype = np.float32
    # 取值压在 [0.9, 1.1)：32 连乘仍接近 1，避免 float32 连乘溢出/精度崩塌
    input1 = np.random.uniform(0.9, 1.1, size=[row, col]).astype(dtype)
    golden = np.prod(input1, axis=0).reshape(1, col)   # 列乘积，1×col
    NumExt.write_array("input.bin", input1, dtype)
    NumExt.write_array("golden.bin", golden, dtype)

if __name__ == "__main__":
    gen_golden_data(32, 32)   # 目录名须等于 "TCOLPRODTest.case_float_32x32"
    # 实际请按 tadd 的写法：os.makedirs(case_name) 后 chdir 进去再生成
```

> 注意两点：① case 目录名/`TEST_F` 名/gen_data 里的名字三处必须逐字符一致；② 造数取值区间要避开「连乘溢出」——`randint(1,10)` 连乘 32 次会超出 float32 范围，这是规约类指令造数与逐元素指令最大的差异。

**（4）`CMakeLists.txt`**：照抄 [tadd/CMakeLists.txt:10](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/CMakeLists.txt#L10)，只改参数为 `pto_cpu_sim_st(tcolprod)`。

### 5.2 步骤二：注册并运行

1. 在 [tests/cpu/st/testcase/CMakeLists.txt](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/CMakeLists.txt) 的 `ALL_TESTCASES` 中按字母序插入一行 `tcolprod`（应落在 `tcolmin` 与 `tcolsum` 之间）。
2. 运行：
   ```bash
   python3 tests/run_cpu.py -t tcolprod --verbose
   ```
3. **需要观察的现象**：cmake 只配置 `tcolprod` 一个目标；`build/bin/tcolprod` 生成；`build/TCOLPRODTest.case_float_32x32/` 下出现 `input.bin`（32×32×4 字节 = 4096）、`golden.bin` 与 `output.bin`（各 32×4 = 128 字节）；gtest 输出 1 个 case PASSED；汇总表出现 `tcolprod | PASS`。
4. **预期结果**：`ResultCmp` 打印 `max diff` 在 1e-5 量级（32 次乘法的 float32 舍入累积），远小于 0.001 容差，用例通过。运行结果待本地验证。
5. **反向验证（强烈建议）**：把 gen_data 的 golden 公式故意改成 `np.sum(..., axis=0)` 再跑，用例应 FAIL 且 `ResultCmp` 打印首个错值下标——这验证比对链路真的在工作，而不是「两边写错抵消」。

### 5.3 延伸（可选）

- 给 `gen_data.py` 加第二个 case `case_float_64x32`（并在 kernel 显式实例化与 `TEST_F` 同步登记），练习「一处语义、四处同步」的改法。
- 阅读指令 CPU 实现 [TColProd.hpp:30-34](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TColProd.hpp#L30-L34)，确认 `TCOLPROD_IMPL` 以 `srcTile.GetValidRow()/GetValidCol()` 为规约范围——然后构造「有效行 < 容量行」的 tile（构造参数传小一点），验证 golden 只需对前 `valid_row` 行求积即可通过，加深对有效区语义（u2-l2、u4-l2）的理解。

## 6. 本讲小结

- PTO 的 ST 组织是「**一条指令一个目录一个 gtest 可执行**」，四件套 = `<op>_kernel.cpp`（被测计算 + 显式实例化）+ `main.cpp`（读入→Launch→写回→比对）+ `gen_data.py`（numpy golden）+ `CMakeLists.txt`（一行注册）；CPU 树与 NPU 树同构，仅注册函数（`pto_cpu_sim_st` / `pto_vec_st`）不同。
- 命名是三方契约：目录名 = 指令小写；`TEST_F` 套件/case 名 = gen_data 建的数据目录名 = gtest 运行时 `GetGoldenDir()` 拼出的寻址路径，任何一处拼错都会导致「读不到数据」或「匹配 0 个测试」。
- `run_st.py`（NPU/sim）与 `run_cpu.py`（CPU 仿真）共享「构建（`-DTEST_CASE` 过滤）→ 拷 gen_data 到 build 执行 → 于 build/bin 跑 gtest（`-g` 过滤）」骨架；CPU 入口额外做增量构建判断（CMakeCache 的 `TEST_CASE` 与 `ALL_TESTCASES` 差集）。
- `validate_op_coverage.py` 用集合差保证「有用例必有入口」，`validate_testcase_names.py` 用宏展开比对保证「入口引用的名字真实存在」，其 `--check-run` 模式专抓「filter 匹配 0 个测试仍退出 0」的假覆盖。
- C++ 实现与 numpy golden 互为镜像，**两边必须成对修改**；规约类指令造数要额外考虑连乘的溢出与精度累积。
- 新增用例的动作清单：建目录四件套 → `ALL_TESTCASES` 登记 → `run_cpu.py -t` 验证 →（NPU 用例还需）`run_st.sh` 加引用并通过两个校验脚本。

## 7. 下一步学习建议

本讲你掌握了「用例怎么写、怎么跑、怎么被守护」。接下来：

1. **u10-l2（CPU 仿真器内幕）**：main.cpp 里那些 `aclrtMalloc` 桩背后是什么——`NPUMemoryModel` 如何模拟 GM/UB/L1 存储层级、多核如何被单线程模拟。
2. **u10-l3（CostModel）**：功能对之后如何预估性能，`tests/run_costmodel.py` 与本讲的 ST 框架是什么关系。
3. **u11-l1（新增一条指令）**：把本讲的「新增用例」扩展成「新增指令」的完整闭环——CPU 头文件、NPU 头文件、ISA 文档、ST 用例四位一体，本讲的四件套正是其中「测试」一环。
4. 想加深本讲内容，可通读 [tests/script/README.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/script/README.md) 与 [tests/README.md:51-157](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/README.md#L51-L157)（通信 ST 的 MPI 约定），并浏览一个非逐元素指令的用例（如 `tests/cpu/st/testcase/tmatmul/`）看四件套在 Cube 类指令上的变体。
