# ST 测试体系：用例、golden 数据与测试脚本

## 1. 本讲目标

学完本讲,你应该能够:

1. 说出一个 ST(Single-instruction Test,单指令测试)用例目录的「四件套」构成,以及四个文件之间靠什么约定互相衔接。
2. 读懂 `main.cpp` 中一个 `TEST_F` 用例从 aclrt 初始化到 golden 比对的完整生命周期。
3. 理解 `gen_data.py` 如何用「用例名 = golden 目录名」的命名契约生成输入与标杆数据,以及 `ResultCmp` 容差比对和 `ResultCmpExact` 精确比对各自的判定模型。
4. 会用 `run_st.py` 的 `-r/-v/-t/-g` 参数组合驱动 NPU(sim/npu)ST,并知道 CPU 模拟器对应的入口是 `run_cpu.py -t/-g`。
5. 了解 `run_st.sh` 冒烟清单(`--simple`/`--all`)如何圈定每个后端的最小用例集,以及本版本(0dbecbe..be5ccb7)A5 ST 用例大批扩容(int64/uint64 用例、`custom_name` 机制、新增 `tpushpop_subblock_dispatch` 用例)的组织方式。

## 2. 前置知识

- **gtest 基础**:ST 用例基于 GoogleTest。`TEST_F(Suite, case)` 注册一个测试,`testing::UnitTest::GetInstance()->current_test_info()` 能在测试体内拿到当前正在跑的 `套件名.用例名`——这是 golden 目录约定的基础。不熟悉 gtest 也无妨,把它理解成「把一堆用例注册进一个可执行文件,逐个运行并报告 PASS/FAIL」即可。
- **host/device 两侧模型**:ST 的 `main.cpp` 运行在 host(CPU)侧,负责准备数据、启动内核、搬运结果;真正的 PTO 指令运行在 device 侧。host 侧通过 `aclrt*` 系列 API(`aclrtMalloc`/`aclrtMemcpy`/`aclrtSynchronizeStream` 等)管理设备内存。在 CPU 模拟器上这些 API 是 stub 替身(u1-l5 讲过 `cpu_stub.hpp`),在 NPU 上是真实的 ascendcl 运行时。
- **golden 比对思路**:测试前用 Python(numpy)按指令的数学语义算出一份「标准答案」`golden.bin`;内核跑完后把输出写回 `output.bin`;C++ 侧逐元素比对两者。浮点运算有舍入误差,所以浮点用带容差的 `ResultCmp`,整数(尤其是位运算)用逐位精确的 `ResultCmpExact`。
- **承接前讲**:u1-l4 已经逐行精读过 tadd 的内核代码(TASSIGN/TLOAD/TADD/TSTORE 与事件同步),本讲不再重复内核细节,而是聚焦「这套测试是如何组织、生成、驱动和比对的」。u1-l2 讲过 `run_cpu.py` 的整体流程(构建→gen_data→gtest),本讲把它拆到文件级。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `tests/cpu/st/testcase/tadd/main.cpp` | CPU 侧 ST 的 gtest 用例骨架(aclrt 生命周期 + golden 比对) |
| `tests/cpu/st/testcase/tadd/gen_data.py` | 用 Python 生成 input/golden 二进制,命名即目录 |
| `tests/cpu/st/testcase/tadd/CMakeLists.txt` | 一行调用 `pto_cpu_sim_st(tadd)` 注册构建目标 |
| `tests/cpu/st/testcase/CMakeLists.txt` | 定义 `pto_cpu_sim_st` 函数与 `ALL_TESTCASES` 清单,按 `TEST_CASE` 过滤 |
| `tests/common/test_common.h` | 各后端共享的测试公共层:`ReadFile`/`WriteFile`/`ResultCmp`/`ResultCmpExact` |
| `tests/script/run_st.py` | NPU ST 驱动:构建→生成 golden→运行二进制(支持 `-g` 过滤) |
| `tests/script/build_st.py` | 只构建不运行(`run_st.sh` 先调它,再用 `run_st.py -w` 复用产物) |
| `tests/run_st.sh` | 一键脚本:`--a3/--a5` × `--simple/--all` × `--sim/--npu` 的冒烟清单 |
| `tests/run_cpu.py` | CPU 模拟器侧入口,`-t/-g` 语义与 `run_st.py` 同构 |
| `tests/npu/a5/src/st/testcase/CMakeLists.txt` | A5 ST 的构建函数(`pto_vec_st`/`pto_cube_st`)与用例清单 |
| `tests/npu/a5/src/st/testcase/tadd/main.cpp`、`tsel/main.cpp` | A5 侧 ST 组织样本(本版本扩容的典型) |
| `tests/README.md` | 测试目录布局与入口速查 |

一个总览先记住:**CPU 侧 ST 住在 `tests/cpu/st/testcase/<指令名>/`,NPU 侧按代际分家——A2/A3 在 `tests/npu/a2a3/src/st/testcase/`,A5 在 `tests/npu/a5/src/st/testcase/`(当前约 159 个用例目录,CPU 侧约 136 个)**。目录名就是指令名的小写,这套「指令定位法」在 u1-l3 已经建立。

## 4. 核心概念与源码讲解

### 4.1 gtest 用例骨架:四件套与 main.cpp 生命周期

#### 4.1.1 概念说明

一个 ST 用例目录由「四件套」构成,靠三条约定衔接:

```
tadd/
├── main.cpp          # host 侧:gtest 用例、aclrt 资源管理、golden 比对
├── tadd_kernel.cpp   # device 侧:LaunchTAdd<T,...> 模板,真正调用 PTO 指令
├── gen_data.py       # 数据生成:为每个 TEST_F 用例生成一个数据目录
└── CMakeLists.txt    # 一行注册:pto_cpu_sim_st(tadd)
```

约定一:**TEST_F 的用例名 = gen_data.py 生成的数据目录名**。`main.cpp` 在运行时用 gtest 反射拿到当前用例名,拼出 `../TADDTest.case_xxx` 这样的相对路径去找 `input*.bin`/`golden.bin`。

约定二:**main.cpp 只声明、kernel 文件只定义**。`main.cpp` 前向声明 `LaunchTAdd` 模板,定义在 `tadd_kernel.cpp`,两侧靠链接(或 A5 上的动态库,见 4.5)拼成完整程序。

约定三:**目录名 = 目标名 = 指令名**。`CMakeLists.txt` 里一行 `pto_cpu_sim_st(tadd)`,同时向 `tests/cpu/st/testcase/CMakeLists.txt` 的 `ALL_TESTCASES` 清单注册目录,才能被 `TEST_CASE` 过滤逻辑看到。

#### 4.1.2 核心流程

一个 `TEST_F` 用例的执行流程(host 视角):

```
test_tadd<T,...>()
 ├─ aclInit / aclrtSetDevice / aclrtCreateStream        # 初始化运行时
 ├─ aclrtMallocHost ×3 + aclrtMalloc ×3 + Memset        # host/设备内存
 ├─ ReadFile("../TADDTest.case_x/input1.bin") ...       # 从 golden 目录读输入
 ├─ aclrtMemcpy H2D                                     # 输入搬上设备
 ├─ LaunchTAdd<T,...>(dst, src0, src1, stream)          # 启动内核(device 侧执行 PTO 指令)
 ├─ aclrtSynchronizeStream + aclrtMemcpy D2H            # 等完成、取回输出
 ├─ WriteFile(".../output.bin")                         # 落盘,便于失败时排查
 ├─ aclrtFree / FreeHost / DestroyStream / ResetDevice / aclFinalize
 └─ ReadFile golden.bin + output.bin → ResultCmp        # 比对
```

构建期的过滤流程:`run_cpu.py`/`build_st.py` 把 `-t tadd` 变成 CMake 变量 `-DTEST_CASE=tadd`,`testcase/CMakeLists.txt` 的 `foreach` 只对匹配项(或未定义时对全部)`add_subdirectory`。

#### 4.1.3 源码精读

先看 golden 目录约定的实现——gtest 反射取当前用例名:

[tests/cpu/st/testcase/tadd/main.cpp:L27-L34](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/cpu/st/testcase/tadd/main.cpp#L27-L34)

`GetGoldenDir()` 返回 `"../" + 套件名 + "." + 用例名`。可执行文件运行于 `build/bin/` 下,所以 `../TADDTest.case_float_64x64_64x64_64x64` 正好落在 `build/` 里 gen_data.py 建好的数据目录上(见 4.2)。

用例主体是一个函数模板,参数把 dtype 与形状编译期化:

[tests/cpu/st/testcase/tadd/main.cpp:L44-L47](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/cpu/st/testcase/tadd/main.cpp#L44-L47)

上面这 4 行完成 aclrt 初始化;下面两段是数据准备与内核启动:

[tests/cpu/st/testcase/tadd/main.cpp:L52-L66](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/cpu/st/testcase/tadd/main.cpp#L52-L66)

注意第 61-62 行的 `ReadFile` 从 `GetGoldenDir()` 读输入——用例名决定数据来源;第 66 行 `LaunchTAdd` 是唯一与 device 侧交接的点。

收尾:同步流、回写 output、释放资源、比对:

[tests/cpu/st/testcase/tadd/main.cpp:L68-L91](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/cpu/st/testcase/tadd/main.cpp#L68-L91)

最后是 TEST_F 注册——每个用例一行,dtype × 形状就是测试矩阵:

[tests/cpu/st/testcase/tadd/main.cpp:L94-L101](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/cpu/st/testcase/tadd/main.cpp#L94-L101)

注意第 99-101 行:bfloat16 用例包在 `#ifdef CPU_SIM_BFLOAT_ENABLED` 里,是否编译由环境变量 `PTO_CPU_SIM_ENABLE_BF16`(经 CMake 翻译成该宏)决定——u2-l1 讲过 bfloat16_t 的支持依赖本机工具链能力,这里就是它在测试体系里的落点。

再看构建注册。用例目录自己的 CMakeLists 只有一行:

[tests/cpu/st/testcase/tadd/CMakeLists.txt:L10-L10](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/cpu/st/testcase/tadd/CMakeLists.txt#L10-L10)

`pto_cpu_sim_st` 函数把 `main.cpp`(加上若存在的 `<name>_kernel.cpp`)编成一个可执行文件,并接好 include 路径与 gtest:

[tests/cpu/st/testcase/CMakeLists.txt:L11-L35](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/cpu/st/testcase/CMakeLists.txt#L11-L35)

第 12-15 行值得注意:`FILES_LIST` 固定含 `main.cpp`,`<NAME>_kernel.cpp` 存在才追加——这就是「四件套」在构建系统里的表达。函数下方是 `ALL_TESTCASES` 清单(第 39 行起,`tmax`、`tadd` 等都在其中),清单末尾按 `TEST_CASE` 过滤:

[tests/cpu/st/testcase/CMakeLists.txt:L166-L170](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/cpu/st/testcase/CMakeLists.txt#L166-L170)

`TEST_CASE` 未定义时构建全部;定义了就只构建同名目录。**新建用例目录后必须把目录名加进这份清单**,否则 `-t` 传了也不会被构建。

#### 4.1.4 代码实践

1. **实践目标**:不看运行,仅凭静态阅读画出一个 ST 用例的「文件-数据-构建」三向关系图。
2. **操作步骤**:打开 `tests/cpu/st/testcase/tadd/` 四件套;在 `main.cpp` 中找出 `GetGoldenDir` 的调用点(共 5 处:input1/input2/golden/output 等);在 `tadd_kernel.cpp` 中找到 `LaunchTAdd` 的定义与显式实例化列表;对照 `tests/cpu/st/testcase/CMakeLists.txt` 的 `ALL_TESTCASES` 里的 `tadd`。
3. **需要观察的现象**:用例名 `case_float_64x64_64x64_64x64` 中的三段数字(全局形状/tile 形状/有效区域)如何同时出现在 `TEST_F` 实参、`gen_data.py` 的 `TAddParams` 与内核实例化中。
4. **预期结果**:三处各有一行与该用例一一对应——这正是 u1-l4 总结的「新增用例须同步改三处」的结构性原因。

#### 4.1.5 小练习与答案

**练习 1**:`GetGoldenDir()` 返回的是 `"../" + suite + "." + case`,为什么前缀是 `../`?
**答案**:可执行文件位于 `<st 根>/build/bin/` 下运行,gen_data.py 生成的数据目录位于 `<st 根>/build/` 下,`../` 恰好从 `bin/` 回退到 `build/`。路径契约由驱动脚本统一保证(run_st.py 的 `run_gen_data` 与 `run_binary` 分别在 `build/` 和 `build/bin/` 下执行,见 4.4)。

**练习 2**:如果把 `TEST_F(TADDTest, case_float_64x64_64x64_64x64)` 改名为 `case_float_32x128` 而不改其它文件,会发生什么?
**答案**:构建仍通过,但运行时 `GetGoldenDir()` 会去找 `../TADDTest.case_float_32x128/input1.bin`,该目录不存在,`ReadFile` 失败,`CHECK_RESULT_GTEST` 使用例 FAIL。用例名、数据目录名、gen_data 参数三者必须同步修改。

**练习 3**:为什么 `main.cpp` 里 bfloat16 用例要条件编译,而 float/int32 不用?
**答案**:CPU 模拟器上 `bfloat16_t` 的可用性取决于本机编译器是否支持( u2-l1 讲过按工具链能力三档取值),所以由 `PTO_CPU_SIM_ENABLE_BF16=1` 环境变量控制宏 `CPU_SIM_BFLOAT_ENABLED` 再决定是否编译该用例;float/int32 在所有平台都可用,无需开关。

### 4.2 golden 数据生成:gen_data.py 的命名契约

#### 4.2.1 概念说明

`gen_data.py` 是 ST 的「出题人」:它为每个 `TEST_F` 用例生成一个同名目录,内含 `input*.bin`(输入)与 `golden.bin`(标准答案)。它解决两个问题:一是**可复现**——固定随机种子,任何人在任何机器上生成的数据一致;二是**语义正确**——golden 由 numpy 按指令的数学定义直接计算,与被测内核的实现路径完全独立,所以能充当裁判。

CPU 侧的 gen_data.py 共享一个工具模块 `utils.py`(位于 `tests/cpu/st/utils.py`),提供 `NumExt`:统一的 dtype 写盘(`write_array`)、短类型名(`get_short_type_name`,如 `np.float32 → "float"`)、bf16 支持等。

#### 4.2.2 核心流程

```
gen_data.py(在 <st 根>/build/ 下被执行)
 ├─ 逐个遍历 case_params_list 中的参数对象
 ├─ generate_case_name(param) → "TADDTest.case_float_64x64_64x64_64x64"
 ├─ os.makedirs(case_name) + os.chdir(case_name)
 ├─ 生成随机输入(固定种子 np.random.seed(19))
 ├─ 用 numpy 按指令语义算 golden(只填有效区域)
 └─ input1.bin / input2.bin / golden.bin 落盘
```

用例名的命名规则:`case_<dtype>_<全局行x列>_<tile行x列>_<有效行x列>`,由参数对象自动拼出;dtype 字符串与 C++ 侧 `TEST_F` 名中的类型别名(float/half/int16/int32/bf16)一一对应。本版本 A5 侧新增了 `custom_name` 机制:当自动命名的形状串无法表达用例语义(如 `inplace` 原地变体)时,允许参数对象显式指定完整用例名(见 4.5.3)。

#### 4.2.3 源码精读

golden 计算的核心——只填有效区域,这是 u2-l3「容量形状 vs 有效区域」在数据侧的镜像:

[tests/cpu/st/testcase/tadd/gen_data.py:L21-L38](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/cpu/st/testcase/tadd/gen_data.py#L21-L38)

第 32-33 行:`golden` 先置零,再只在 `[:row_valid,:col_valid]` 区间填 `input1+input2`。因为 TSTORE 只写有效区,有效区外的 golden 值不会被内核触碰,置零即安全。

参数对象与命名规则:

[tests/cpu/st/testcase/tadd/gen_data.py:L42-L64](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/cpu/st/testcase/tadd/gen_data.py#L42-L64)

主入口:固定种子、按清单批量建目录:

[tests/cpu/st/testcase/tadd/gen_data.py:L67-L92](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/cpu/st/testcase/tadd/gen_data.py#L67-L92)

第 17 行的 `np.random.seed(19)` 在文件头部——所有 tadd 输入都是同一序列的伪随机数,保证可复现。第 87-92 行:对每个参数建同名目录、`chdir` 进去生成,所以 `gen_data.py` 的**执行目录**决定了数据落点;驱动脚本把它拷贝到 `build/` 下执行(4.4.3),于是数据目录都落在 `build/<用例名>/`。

#### 4.2.4 代码实践

1. **实践目标**:亲手验证「gen_data.py 的执行目录 = 数据目录」。
2. **操作步骤**:在一个临时目录里执行 `mkdir -p /tmp/gen_practice && cp tests/cpu/st/testcase/tadd/gen_data.py /tmp/gen_practice/ && cd /tmp/gen_practice && python3 gen_data.py`(需要 numpy;`utils` 模块若导入失败,可临时把 `tests/cpu/st/` 加进 `PYTHONPATH`,因为 `utils.py` 在那里)。然后 `ls` 查看。
3. **需要观察的现象**:当前目录下出现 `TADDTest.case_float_64x64_64x64_64x64/` 等目录(默认 4 个;设 `PTO_CPU_SIM_ENABLE_BF16=1` 则 5 个),每个目录内有 `input1.bin`、`input2.bin`、`golden.bin`。
4. **预期结果**:目录名与 `main.cpp` 的 `TEST_F` 用例名逐字一致;用 `xxd` 查看 `golden.bin` 大小应为 `64*64*4` 字节(float32 用例)。若 `utils` 导入失败,说明你绕过了驱动脚本的目录布局——正常,正式运行时脚本会保证相对位置(待本地验证)。

#### 4.2.5 小练习与答案

**练习 1**:golden 为什么只填 `[:row_valid,:col_valid]`,其余置零?
**答案**:PTO 指令按 Tile 的有效区域工作(计算只扫有效区、TSTORE 只写有效区),有效区外的输出缓冲不受内核影响。golden 在无效区置零与「输出缓冲被 `aclrtMemset` 清零」对应,比对时双方一致;若在无效区放了随机值,反而会因内核不写该区域而误报失败。

**练习 2**:两个不同用例(如 `case_float_64x64` 与 `case_int32_64x64`)的输入数值相同吗?
**答案**:相同。`np.random.seed(19)` 在进程级只播种一次,`randint(1,10)` 按顺序消费同一随机序列,再用 `NumExt.astype` 转换成各自 dtype。这既是可复现性的来源,也意味着同文件内用例的输入之间存在顺序依赖——调整用例顺序会改变所有后续用例的数据(但 golden 同步重算,不影响正确性)。

**练习 3**:本版本 A5 的 tadd gen_data.py 为 `inplace` 用例增加了一段 golden 修正逻辑,它解决什么问题?
**答案**:原地(in-place)变体中,输出缓冲就是 `input1`,有效列之外的元素不会被内核改写,因此 golden 的无效区必须保留 `input1` 的原值而不是置零。相关代码在 [tests/npu/a5/src/st/testcase/tadd/gen_data.py:L39-L40](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/src/st/testcase/tadd/gen_data.py#L39-L40)(`if "inplace" in case_name and w_valid < dst_tile_col` 时把无效区填回 `input1`)。

### 4.3 结果比对:ResultCmp 容差模型与 ResultCmpExact

#### 4.3.1 概念说明

比对函数定义在 `tests/common/test_common.h`,**CPU、A2/A3、A5 等所有后端的 ST 共用这一份**(各后端 CMake 都把 `tests/common`(或 `tests/npu/<soc>/src/common`)加进 include 路径)。两类比对:

- `ResultCmp<T>(期望, 实际, eps)`:面向浮点。逐元素比较,允许「绝对误差与相对误差都超过 eps」的元素少量存在,还统计「期望非零、实际为零」的元素数(捕捉整体算错的病态输出)。
- `ResultCmpExact(期望, 实际)`:面向整数。逐元素严格相等,`static_assert(std::is_integral_v<T>)` 在编译期拒绝浮点类型。位运算、移位、int64 加法这类「没有舍入理由」的指令必须用它。

#### 4.3.2 核心流程

`ResultCmp` 的判定模型:

- 单元素判错条件:`(diff > eps 且 relRatio > eps)` 或 `(期望与实际的 NaN 状态不一致)`;其中 `diff = |exp-act|`,`relRatio = diff/|exp|`。
- 全局失败条件:`errCount > threshold` 或 `zeroCount > zeroCountThreshold(默认 1000)`。
- `threshold` 默认取 `eps × 元素总数`,即**允许的错误元素比例约等于 eps**(tadd 用 `eps=0.001`,即允许约千分之一的元素超差)。

`zeroCount` 是防「输出大面积为零」的哨兵:期望非零而实际为零的元素超过 1000 个即失败——这通常意味着内核根本没写输出(如搬运/事件配错),而不是个别浮点误差。

#### 4.3.3 源码精读

精确比对——首个错误即打印,统计错误总数:

[tests/common/test_common.h:L213-L229](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/common/test_common.h#L213-L229)

第 216 行的 `static_assert` 保证它只用于整数类型;第 221-222 行只打印第一个错误元素(避免刷屏),第 227 行输出汇总。

容差比对的核心循环:

[tests/common/test_common.h:L231-L269](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/common/test_common.h#L231-L269)

第 236 行:`threshold` 未显式给定时取 `size × eps`;第 259-261 行是单元素判错的三条件;第 256 行累计 `zeroCount`。循环结束后打印 `max diff / err count / zero count` 等统计(第 274-278 行)——失败时先看这行日志,能立刻区分「个别超差」还是「整体为零」。

`main.cpp` 侧的调用与失败兜底(读文件失败也要让用例失败而不是崩溃):

[tests/cpu/st/testcase/tadd/main.cpp:L84-L91](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/cpu/st/testcase/tadd/main.cpp#L84-L91)

其中 `CHECK_RESULT_GTEST` 宏(定义于 [tests/common/test_common.h:L40-L44](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/common/test_common.h#L40-L44))把 `ReadFile` 的返回码翻译成 `EXPECT_TRUE` 断言。

#### 4.3.4 代码实践

1. **实践目标**:从一次真实比对输出中读出失败模式。
2. **操作步骤**:在 CPU 模拟器上跑通 tadd(`python3 tests/run_cpu.py -t tadd`,待本地验证);然后临时把 `tests/cpu/st/testcase/tadd/main.cpp` 第 89 行的 `0.001f` 改成 `0.0f` 重新运行,观察日志变化(仅作阅读实验,改完记得还原,不要提交)。
3. **需要观察的现象**:正常通过时日志形如 `max diff: 0, err count: 0 ...`;把 eps 收紧到 0 后,若有个别元素因浮点舍入超差,`err count` 变为非零,且 `err count ratio` 给出占比。
4. **预期结果**:`threshold = size × eps`,eps=0 时 threshold=0,任何一个超差元素都会让 `ResultCmp` 返回 false、用例 FAIL——这演示了容差参数如何控制严格程度(int 指令无此自由度,必须走 `ResultCmpExact`)。

#### 4.3.5 小练习与答案

**练习 1**:为什么 int64 的 TADD 用例用 `ResultCmpExact` 而不是 `ResultCmp`?
**答案**:整数加法在数学上是精确的,任何一位不相等都是真 bug;`ResultCmp` 的容差与错误配额反而会掩盖错误。且 `ResultCmp` 内部把元素转成 `float` 再比较(第 246-247 行 `static_cast<float>`),int64 超出 float 的 24 位尾数精度,本身就比不了。

**练习 2**:`zeroCountThreshold` 防的是什么故障?
**答案**:防止「输出大面积为零」的病态通过或病态失败——典型场景是事件链配错导致 TSTORE 根本没执行、或 tile 地址规划重叠互相覆盖,此时绝大多数元素期望非零而实际为零。它让这类结构性错误立刻暴露,而不是靠逐元素 diff 慢慢积累超阈值。

**练习 3**:比对用的 `golden.bin` 和 `output.bin` 都在用例目录里,`output.bin` 会被提交进仓库吗?
**答案**:不会,它是每次运行的产物,价值在于失败时可以离线 diff(用 `xxd`/numpy 对照 golden 定位出错元素);仓库里跟踪的是 `gen_data.py` 脚本本身,数据目录由脚本按需生成。

### 4.4 run_st.py 驱动:参数、目录路由与三段式执行

#### 4.4.1 概念说明

`run_st.py` 是 **NPU 侧** ST 的统一驱动(注意:它不驱动 CPU 模拟器)。一次调用完成三段式流程:**构建**(cmake + make,`-DTEST_CASE` 过滤)→ **生成标杆**(把用例的 gen_data.py 拷到 build 下执行)→ **运行二进制**(chdir 到 `build/bin/`,按 `-g` 附加 `--gtest_filter`)。`-r sim` 用 NPU 仿真器(camodel,仍需 CANN 环境),`-r npu` 上真机。

**CPU 模拟器的对应入口是 `run_cpu.py`**,其 `-t/--testcase` 与 `-g/--gtest_filter` 参数(定义于 [tests/run_cpu.py:L454-L455](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/run_cpu.py#L454-L455))与 `run_st.py` 同构——两个脚本一套心智模型,只是面向的后端不同。`tests/README.md` 把两者并列列出:[tests/README.md:L9-L13](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/README.md#L9-L13)。

#### 4.4.2 核心流程

```
python3 tests/script/run_st.py -r sim -v a3 -t tadd -g TADDTest.case_xxx
 ├─ 解析参数:-r 运行模式 / -v SoC / -t 用例 / -g gtest 过滤
 ├─ soc_version 映射:a3→Ascend910B1、a5→Ascend910_9599、a6→dav_9201...
 ├─ 目录路由:-v a3 → tests/npu/a2a3/src/st;-v a5 → tests/npu/a5/src/st
 │            comm/ 前缀 → <soc>/comm/st(is_comm)
 ├─ set_env_variables:sim 模式注入 simulator 的 LD_LIBRARY_PATH
 ├─ build_project:cmake -DRUN_MODE -DSOC_VERSION -DTEST_CASE .. && make -j
 ├─ run_gen_data:cp testcase/<t>/gen_data.py build/ && (cd build && python3 gen_data.py)
 └─ run_binary:(cd build/bin && ./<t> --gtest_filter=<g>)
```

`run_st.sh` 的冒烟脚本采用「先 `build_st.py -t all` 全量构建一次,再逐条 `run_st.py -w`(不带构建)运行」的组合,`-w/--without-build` 跳过编译、只清掉旧的 `build/T*` 数据目录后直接运行。

#### 4.4.3 源码精读

参数定义——本讲最常用的一组开关:

[tests/script/run_st.py:L273-L284](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/script/run_st.py#L273-L284)

`-v` 的别名映射与目录路由(决定了「同一份脚本命令,不同代际落到不同目录树」):

[tests/script/run_st.py:L287-L327](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/script/run_st.py#L287-L327)

第 288-297 行:`a5` 映射到 `Ascend910_9599`、`a6` 映射到 `dav_9201`;第 314-327 行:`is_comm` 与 `-v` 共同决定 `target_dir`。这就是「同一指令的用例在 a2a3 与 a5 各有一套目录」在驱动侧的体现。

构建段把 `-t` 翻译成 CMake 变量:

[tests/script/run_st.py:L100-L118](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/script/run_st.py#L100-L118)

第 101 行 `-DTEST_CASE={testcase}` 正是 4.1.3 里 `foreach` 过滤的输入端;`-t all` 则构建全部。

标杆生成段——拷贝到 build 再执行,保证数据目录与二进制相对位置正确:

[tests/script/run_st.py:L128-L144](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/script/run_st.py#L128-L144)

运行段——`-g` 变成 gtest 的命令行过滤:

[tests/script/run_st.py:L223-L237](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/script/run_st.py#L223-L237)

通信用例(`comm/` 前缀)在此函数更下方会经由 `mpirun -n <nranks>` 启动多进程,并按 2/4/8 rank 用 `GTEST_FILTER` 自动分轮(u6 会展开)。

#### 4.4.4 代码实践

1. **实践目标**:掌握 `run_st.py` 的参数组合,并弄清它与 CPU 入口的分工。
2. **操作步骤**:(a) 读 `tests/README.md` 的入口清单;(b) 若本机有 CANN 环境,执行 `python3 tests/script/run_st.py -r sim -v a3 -t tadd -g TADDTest.case_float_64x64_64x64_64x64`(完整跑一次三段式);(c) 无 CANN 环境时,改用 CPU 模拟器等价命令 `python3 tests/run_cpu.py -t tadd -g TADDTest.case_float_64x64_64x64_64x64`。
3. **需要观察的现象**:脚本打印 `target_dir: .../npu/a2a3/src/st`(或 CPU 路径的 build 目录)、`run command: cmake ...`、`run command: python3 gen_data.py`、`run command: ./tadd --gtest_filter=...` 三段日志。
4. **预期结果**:用例 PASS。`-g` 省略时运行该用例目录下全部 `TEST_F`;`-t` 与 `ALL_TESTCASES` 不匹配时 A5 侧 CMake 会直接 `FATAL_ERROR`(CPU 侧则静默不构建任何目标,详见 4.5.3)。本条实践的运行结果待本地验证。

#### 4.4.5 小练习与答案

**练习 1**:`run_st.py -r sim -v a5 -t comm/tput_async` 会路由到哪个目录?
**答案**:`is_comm` 为真且 `-v a5`,按 [tests/script/run_st.py:L314-L315](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/script/run_st.py#L314-L315) 路由到 `tests/npu/a5/comm/st`,并在运行段以 `mpirun` 拉起多进程。非 comm 的 a5 用例则进 `tests/npu/a5/src/st`。

**练习 2**:`-w`(without-build)为什么存在?配合谁使用?
**答案**:冒烟脚本 `run_st.sh` 先用 `build_st.py -t all` 把全部用例构建一遍,随后几十条 `run_st.py -w` 只运行不重编,避免每条用例都全量 cmake/make;`-w` 分支仅清理 `build/T*` 旧数据目录后直接进入 gen_data/run 段。

**练习 3**:为什么说「run_st.py 的 `-r sim` 不等于 CPU 模拟器」?
**答案**:`sim` 指 NPU 仿真器(camodel):二进制仍用 ccec 按 NPU 目标编译、链接 `runtime_camodel`,需要 `ASCEND_HOME_PATH` 指向 CANN 安装(u1-l2 的三种模式区分)。CPU 模拟器(`__CPU_SIM`,本机 g++ 直编)的入口是 `tests/run_cpu.py`,不走 run_st.py。

### 4.5 A5 ST 用例组织:双目标编译、本批扩容与冒烟清单

#### 4.5.1 概念说明

A5 的 ST 与 CPU 的 ST 四件套同名同构,但构建拓扑不同:**CPU 把 `main.cpp` 与 `*_kernel.cpp` 编进同一个可执行文件;A5 把 kernel 编成动态库、把 main 编成可执行文件**——kernel 必须用 ccec(设备编译器,`--cce-aicore-arch=dav-c310-vec`/`-cube`)编译,main 用普通 C++ 编译器编译,两者链接成宿主程序。

本版本(0dbecbe..be5ccb7)A5 ST 出现一次大批扩容,diff 侧面印证了三件事:

1. **int64/uint64 用例批量落地**:数十个用例(tadd/tand/tor/txor/tsel/tcmp/tpartmax/tshl...)的 main.cpp 增加了 64 位 dtype 的 `TEST_F`,配套 gen_data.py 加参数、比对改用 `ResultCmpExact`——这是 u4-l7 讲的 A5 寄存器对仿真的测试侧配套。
2. **`custom_name` 机制**:gen_data.py 的参数对象支持显式用例名,服务 `inplace` 等自动命名表达不了的变体。
3. **新增唯一全新用例目录 `tpushpop_subblock_dispatch`**:验证 TPUSH/TPOP 在 TILE_NO_SPLIT 时固定派发到逻辑 sub-block 0 的修复(u5-l6 承接)。

#### 4.5.2 核心流程

A5 ST 的构建拓扑:

```
pto_vec_st(tadd) / pto_cube_st(tadd)
 ├─ target 1: tadd_kernel.so
 │    源文件   tadd_kernel.cpp
 │    编译器   ccec --cce-aicore-arch=dav-c310-vec(-cube)
 │    include  ${ASCEND_HOME_PATH}/pkg_inc/
 └─ target 2: tadd(可执行)
      源文件   main.cpp(g++ 编译)
      链接     tadd_kernel.so + runtime(sim)/runtime(npu) + gtest
```

int64 用例的四件套同步模式(以 tadd 为例):

```
main.cpp     新增 LaunchTAddInplace 前向声明 + test_tadd_inplace 模板
             + TEST_F(..., case_int64_4x32_inplace) 等注册
             + CheckTAddResult 里 int64/uint64 分流到 ResultCmpExact
kernel.cpp   对应 int64/uint64 模板实例化
gen_data.py  TAddParams 增加 custom_name 用例 + inplace golden 保留无效区原值
run_st.sh    (如需单独冒烟)增加一行 run_st.py 命令
```

#### 4.5.3 源码精读

先看双目标构建函数 `pto_vec_st`:

[tests/npu/a5/src/st/testcase/CMakeLists.txt:L11-L39](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/src/st/testcase/CMakeLists.txt#L11-L39)

第 12 行把 kernel 编成 `SHARED` 库;第 13 行的 `--cce-aicore-arch=dav-c310-vec` 是 A5 向量核的设备架构(-cube 变体在 [L41-L43](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/src/st/testcase/CMakeLists.txt#L41-L43));第 35-36 行按 `RUN_MODE` 在 `runtime_camodel`(sim)与 `runtime`(npu)之间切换链接。用例目录自身仍是一行注册:[tests/npu/a5/src/st/testcase/tadd/CMakeLists.txt:L11-L11](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/src/st/testcase/tadd/CMakeLists.txt#L11-L11)。

再看本批 int64 扩容的比对分流——`CheckTAddResult` 按类型选择精确/容差比对:

[tests/npu/a5/src/st/testcase/tadd/main.cpp:L43-L53](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/src/st/testcase/tadd/main.cpp#L43-L53)

第 50-51 行的 `if constexpr` 让 64 位整数走 `ResultCmpExact`,其余走 0.001 容差。tsel 用例里是同一模式的另一份样本:[tests/npu/a5/src/st/testcase/tsel/main.cpp:L38-L49](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/src/st/testcase/tsel/main.cpp#L38-L49)。

新增的 inplace 用例注册(本批新增的典型形态——形状故意覆盖尾块场景,如 `1x2048` 有效 `2045`):

[tests/npu/a5/src/st/testcase/tadd/main.cpp:L188-L192](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/src/st/testcase/tadd/main.cpp#L188-L192)

gen_data.py 侧的 `custom_name` 用例(自动命名表达不了 `inplace` 语义,于是显式指定完整用例名):

[tests/npu/a5/src/st/testcase/tadd/gen_data.py:L106-L110](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/src/st/testcase/tadd/gen_data.py#L106-L110)

用例清单与构建过滤:新用例目录必须登记进 `ALL_TESTCASES`(本版本在 [L253](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/src/st/testcase/CMakeLists.txt#L253) 登记了 `tpushpop_subblock_dispatch`);与 CPU 侧不同,A5 侧对非法 `TEST_CASE` 显式报错,还有 OPT_IN(仅在点名时才编)与 AUTO_MODE 白名单两类特殊清单:

[tests/npu/a5/src/st/testcase/CMakeLists.txt:L349-L367](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/src/st/testcase/CMakeLists.txt#L349-L367)

最后是冒烟清单。`run_st.sh --a5 --npu --simple` 段的每一行都精确到单个 gtest 用例(比 A3 段更细),本版本在 tpushpop 系列后新增了一行:

[tests/run_st.sh:L898-L902](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/run_st.sh#L898-L902)

`--all` 段则不带 `-g`,整目录全量运行,新增的一行在:

[tests/run_st.sh:L1112-L1114](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/run_st.sh#L1112-L1114)

**CPU ST 与 A5 ST 的 main.cpp 组织差异速查**(实践任务后半的答案框架):

| 维度 | CPU ST(`tests/cpu/st/testcase`) | A5 ST(`tests/npu/a5/src/st/testcase`) |
|---|---|---|
| 编译拓扑 | main.cpp + kernel.cpp → 同一可执行文件 | kernel.cpp → ccec 编成 `.so`;main.cpp → g++ 可执行文件,链接 so |
| 运行时 | aclrt 全是 stub 替身(malloc/memcpy) | 真实 ascendcl;include `"acl/acl.h"` |
| 模板参数 | 通常全局形状 + tile 形状两组 | 常见 dst/src 三个 tile 形状 + vRows/vCols 有效区,更贴近硬件测试矩阵 |
| 比对封装 | 在 `test_tadd` 尾部直接调 `ResultCmp` | 独立的 `CheckTxxResult` 模板,int64/uint64 经 `if constexpr` 分流 `ResultCmpExact` |
| gen_data 工具 | 共享 `tests/cpu/st/utils.py` 的 `NumExt` | 原生 numpy + `tofile`,本批引入 `custom_name` |
| 非法 TEST_CASE | 静默不构建 | CMake `FATAL_ERROR` 显式报错 |

#### 4.5.4 代码实践

1. **实践目标**:建立「同一指令、两套 ST」的对照意识,并能读懂 A5 扩容用例的结构。
2. **操作步骤**:(a) 打开 `tests/npu/a5/src/st/testcase/tcmp/main.cpp` 与 `tpartmax/main.cpp`,定位各自新增的 int64/uint64 `TEST_F` 行与 `ResultCmpExact` 分流;(b) 对照 `tests/cpu/st/testcase/tcmp/main.cpp` 的同名用例,按上面的速查表逐项核对差异;(c) 在 `tests/run_st.sh` 中搜索 `tpartmax`、`tsel`、`tcmp`,确认它们已被 simple 或 all 清单覆盖。
3. **需要观察的现象**:A5 的 `Launch*` 前向声明模板参数表更长(含有效区);`Check*Result` 的 `if constexpr` 分流是本批用例的共同 fingerprint。
4. **预期结果**:能说出「A5 main.cpp = CPU main.cpp 骨架 + 更细的形状矩阵 + 64 位精确比对分流 + 双目标链接」这一句概括。
5. 运行层面的验证需要 CANN/A5 环境,此处为源码阅读型实践,无需设备。

#### 4.5.5 小练习与答案

**练习 1**:为什么 A5 必须把 kernel 与 main 拆成两个编译目标,而 CPU 不用?
**答案**:A5 的 kernel 代码(`tadd_kernel.cpp` 里的 PTO 指令)必须由 ccec 以设备架构(`dav-c310-vec/cube`)编译,而 main.cpp 是 host 侧 g++/clang 代码,两者的编译器、语言扩展与链接约定都不同,只能各自成库再链接。CPU 模拟器上全部代码都是本机 C++(`__CPU_SIM` 宏把昇腾特性替换为替身),单编译器单目标即可。

**练习 2**:`tpushpop_subblock_dispatch` 用例验证什么?为什么值得进冒烟清单?
**答案**:验证 TPUSH/TPOP 在 `TILE_NO_SPLIT` 场景固定派发到逻辑 sub-block 0 的修复(不再读取 `get_subblockid()`,详见 u5-l6)。核间派发类 bug 属于「CPU 模拟器查不出、上板偶发」的高危类型,把它钉进 simple 冒烟清单,保证每次冒烟都回归这处修复。

**练习 3**:`OPT_IN_TESTCASES` 与 `ALL_TESTCASES` 的区别是什么?
**答案**:`ALL_TESTCASES` 默认参与「构建全部」路径;`OPT_IN_TESTCASES`(当前含 `tprefetch_async`、`tquant_dn`)只在显式 `-DTEST_CASE=<name>` 或 `-t all` 时才构建,用于把低频/高成本用例从日常 CI 构建中摘出去,同时保留本地单跑能力。

## 5. 综合实践

**任务:给 TMAX 做一次「微扩容」,完整走一遍 ST 用例维护流程。**

背景事实:`tests/cpu/st/testcase/tmax/` 四件套已存在(main.cpp 100 行、gen_data.py 84 行、tmax_kernel.cpp 66 行),已有 float/int32/int16/half(+条件 bf16)用例。你要做的是本版本 A5 扩容的同款操作,只是发生在 CPU 侧、以形状/dtype 为维度:

1. **跑通现状**:执行 `python3 tests/run_cpu.py -t tmax`,确认全部既有用例 PASS;再用 `-g TMAXTest.case_float_64x64_64x64_64x64` 体会单用例过滤(待本地验证)。
2. **读模板**:对照 [tests/cpu/st/testcase/tmax/main.cpp:L94-L100](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/cpu/st/testcase/tmax/main.cpp#L94-L100) 的 TEST_F 列表与 [tests/cpu/st/testcase/tadd/gen_data.py:L76-L83](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/cpu/st/testcase/tadd/gen_data.py#L76-L83) 的参数清单,确认「一行 TEST_F ↔ 一行参数对象 ↔ kernel 一处实例化」的三处同步。
3. **扩容形状用例**:仿照 `case_half_16x256` 新增 `case_half_32x128`(half 类型、tile 32x128、有效区 32x128):改 main.cpp 加一行 TEST_F、gen_data.py 加一行 `TMaxParams`、tmax_kernel.cpp 按文件内既有实例化的样式补对应模板实例化;golden 语义即 `np.maximum(input1, input2)`(gen_data.py 内应已有对应实现,确认即可)。
4. **(选做)尝试 dtype 扩容**:再加一个 `uint8` 用例。若编译或运行报错,对照 u4-l3 讲过的检查层分析是哪一层拦截(dtype 白名单在 NPU 后端 Check 里,CPU 侧通用模板多半能过)——无论成败都记录结论;失败就回退到第 3 步的形状变体(待本地验证)。
5. **A5 对照**:浏览 `tests/npu/a5/src/st/testcase/tsel/`、`tcmp/`、`tpartmax/` 三个目录的 main.cpp,按 4.5.3 的速查表写 5 条「A5 与 CPU 组织差异」笔记;并在 `tests/run_st.sh` 中找到这批用例对应的冒烟行。
6. **验收标准**:新用例在 CPU 模拟器上 PASS;`git status` 显示只改了 tmax 目录内三个文件(不要动 `ALL_TESTCASES`——目录已注册);输出一份「三处同步点」清单贴在你的笔记里。

## 6. 本讲小结

- ST 用例 = 四件套(`main.cpp`/`<指令>_kernel.cpp`/`gen_data.py`/`CMakeLists.txt`),靠「TEST_F 用例名 = golden 数据目录名 = gen_data 参数」的命名契约衔接,新增/修改用例必须三处同步。
- `main.cpp` 的用例生命周期:aclrt 初始化 → host/device 内存 → 读 input → H2D → Launch → 同步回读 → 写 output → 比对;`GetGoldenDir()` 用 gtest 反射把运行期用例名映射到 `../套件.用例` 数据目录。
- 比对分两类:浮点走 `ResultCmp`(eps 容差 + 错误配额 `size×eps` + zeroCount 哨兵),整数走 `ResultCmpExact`(逐位精确,`static_assert` 拒绝浮点);公共实现在各后端共享的 `tests/common/test_common.h`。
- `run_st.py -r/-v/-t/-g` 驱动 NPU(sim/npu)ST 的「构建→gen_data→运行」三段式,并按 `-v` 路由到 a2a3/a5 目录树;CPU 模拟器的同构入口是 `run_cpu.py -t/-g`;`-r sim` 是 NPU 仿真器而非 CPU 模拟器。
- `run_st.sh --simple/--all` 是冒烟清单:simple 精确到单个 gtest 用例(先 `build_st.py -t all` 再逐条 `run_st.py -w` 复用构建),all 整目录运行;A5 侧对非法 `-t` 会 CMake `FATAL_ERROR`。
- 本版本 A5 ST 大批扩容三件事:int64/uint64 用例伴随寄存器对仿真落地(`CheckTxxResult` + `ResultCmpExact` 分流)、gen_data.py 的 `custom_name` 机制、新增 `tpushpop_subblock_dispatch` 用例并进入 simple/all 双清单。

## 7. 下一步学习建议

- **u5-l2(写一个融合算子)**:把 ST 里学到的「数据生成 + golden 比对」方法带进多指令组合的算子开发,体会多 tile 的 UB 规划与事件排列。
- **u5-l6(A5 平台与 MX matmul)**:本讲只覆盖了 `tpushpop_subblock_dispatch` 的测试意图,其背后的 TPUSH/TPOP 派发语义与 A5 平台差异在 u5-l6 展开。
- **u6-l1(通信 ISA 总览)**:`run_st.py` 的 `comm/` 前缀路由、`mpirun` 多进程与按 rank 数自动分轮的机制,在通信 ST 一讲正式展开。
- **源码延伸阅读**:`tests/script/run_cpu.py` 的 ST 模式与 `run_st.py` 逐段对照,巩固「一套心智模型、两个后端入口」;`tests/README.md` 的 Layout 一节是目录速查表。
