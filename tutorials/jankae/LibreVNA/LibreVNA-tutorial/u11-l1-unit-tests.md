# 单元测试与数值验证

## 1. 本讲目标

LibreVNA 的 GUI 里藏着一座"数学工厂"：S 参数在各参数域之间来回换算、FFT 正逆变换、校准误差项求解、端口延伸的相位斜率回归……这些代码有一个共同特点——**写错了一样能跑，只是结果悄悄不对**。一座桥的图纸画错了会塌，一个 `Zparam` 转换公式抄错了一个符号，Smith 图上只会显示一条"看起来不太对劲"的曲线。

本讲带你走进仓库里的 `Software/PC_Application/LibreVNA-Test/` 测试工程，学完之后你应该能够：

1. 说清这个测试工程如何组织：一个 `main.cpp` 聚合六个测试类、`.pro` 文件把整个 GUI 编进测试可执行文件的原因。
2. 编译并运行 LibreVNA-Test，读懂 Qt Test 的输出，并能挑选单个测试函数运行。
3. 掌握数值代码的三种验证套路：**已知答案测试**（手算期望值）、**独立公式对照**（用另一条算法路径当裁判）、**往返一致性**（变过去再变回来）。
4. 理解浮点断言的容差艺术：为什么有的测试用 `qFuzzyCompare(double)`、有的要降级成 `float` 比较、有的干脆用 `1e-14` 绝对容差。
5. 为 `Tools/parameters.h` 中一个**目前完全没有测试覆盖**的函数（`Tparam` 转换）亲手补一个测试，走完"选函数 → 手算 → 写断言 → 登记 → 运行"的完整闭环。

## 2. 前置知识

### 2.1 什么是单元测试

单元测试就是把程序里**最小可独立执行的单位**（通常是一个函数）单独拎出来，喂给它输入，检查输出是否符合预期。C++ 世界里最常见的做法是"断言式"测试：

```
准备输入 → 调用被测函数 → 用断言比较"实际值"和"期望值"
```

断言失败时测试框架记录一条失败并继续（或中止当前用例），全部跑完汇总报告。这样一次编译就能把几十个数学函数的正确性全部"点名"一遍。

### 2.2 Qt Test 框架的三个关键词

LibreVNA 用的是 Qt 自带的测试框架 Qt Test，只需要认识三个东西：

| 关键词 | 作用 |
|---|---|
| `private slots:` | 测试类的**私有槽**就是测试用例。Qt Test 用 moc 反射枚举类里所有私有槽，按声明顺序逐个调用 |
| `QVERIFY(条件)` | 最基本的断言：条件为假则当前测试函数失败 |
| `QTest::qExec(测试对象, argc, argv)` | 驱动一个测试类跑完它全部的槽，返回非零表示有失败 |

所以一个测试类 = 一个 `QObject` 子类 + 一堆私有槽，仅此而已。

### 2.3 数值测试的特殊困难：浮点数没有"相等"

整数可以 `==`，浮点不行。`0.1 + 0.2 != 0.3` 是 IEEE 754 的天性。数值测试必须回答两个问题：

- **期望值从哪来？** 用被测代码自己算一遍再存下来，等于没测（这叫"录音式测试"，只能防回归、不能防原本就错）。正规做法是期望值来自**被测代码之外**的独立来源：手算、教科书公式、物理常识。
- **容差设多大？** 太严会误报（两条数值路径的合法差异被判失败），太松会漏报（真 bug 被吞掉）。Qt 提供的 `qFuzzyCompare` 是**相对**容差比较：

\[ |p_1 - p_2| \times 10^{k} \le \min(|p_1|, |p_2|), \quad k = 12\ (\text{double}),\; 5\ (\text{float}) \]

注意两点：相对容差意味着**一边接近 0 时不可靠**（此时应改用 `qFuzzyIsNull`）；把 double 强转成 float 再比较，等价于把容差从约 \(10^{-12}\) 放宽到约 \(10^{-5}\)——本讲稍后会看到作者在 `parametertests.cpp` 里正是这么干的，并且留了注释解释原因。

### 2.4 数值代码的三种验证套路

本讲的测试文件恰好示范了三种互补的思路，先记住名字：

1. **已知答案测试（golden value）**：挑一个能手算的输入，把期望值硬编码进测试。例如理想直通的 T 参数就是单位矩阵。
2. **独立公式对照（oracle）**：在测试里**用另一条数学路径**现场算出期望值，再和被测函数比。例如 `S2Z_2P` 在测试里手写了教科书上的 \(Z_{11}\) 展开式，与代码里的 Eigen 矩阵算法对质。
3. **往返一致性（round-trip）**：S→ABCD→S，或者 FFT→IFFT，变换过去再变换回来必须还原。抓的是"成对实现的两个函数不是彼此的逆"这类错误。

三种套路强度递减、成本也递减：往返测试不需要期望值，但它测不出"两个方向犯了同一个错"。

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| `Software/PC_Application/LibreVNA-Test/main.cpp` | 测试入口：聚合六个测试类，累积退出码 |
| `Software/PC_Application/LibreVNA-Test/LibreVNA-Test.pro` | 测试工程定义：把整个 GUI + 协议层编进测试 |
| `Software/PC_Application/LibreVNA-Test/parametertests.{h,cpp}` | S/ABCD/Z 参数互转的已知答案与独立公式测试 |
| `Software/PC_Application/LibreVNA-Test/calibrationtests.cpp` | 校准测量频率栅格探测（线性/对数/混合）测试 |
| `Software/PC_Application/LibreVNA-Test/ffttests.cpp` | FFT 手算期望值 + 往返一致性测试 |
| `Software/PC_Application/LibreVNA-Test/utiltests.cpp` | 圆拟合、版本比较、阻抗↔S 参数等工具测试 |
| `Software/PC_Application/LibreVNA-Test/portextensiontests.cpp` | 端口延伸自动计算与修正的端到端测试 |
| `Software/PC_Application/LibreVNA-Test/impedancerenormalizationtests.cpp` | 阻抗再归一化的物理已知值测试 |
| `Software/PC_Application/LibreVNA-GUI/Tools/parameters.{h,cpp}` | 被测对象：参数域转换数学库（本讲实践对象） |
| `Software/PC_Application/LibreVNA-GUI/Traces/fftcomplex.h` | 被测对象：Nayuki FFT 接口声明 |
| `Software/PC_Application/LibreVNA-GUI/Util/util.h` | 被测对象：圆拟合、传输线、版本比较声明 |

## 4. 核心概念与源码讲解

本讲的三个最小模块：**测试工程结构**、**校准与参数测试用例**、**新增用例方法**。

### 4.1 测试工程结构

#### 4.1.1 概念说明

测试工程要回答的第一个问题是：**被测代码怎么进来？** LibreVNA 的 GUI 没有把数学库拆成独立的 `.so`/`.a`，`calibration.cpp` 依赖 `calkit.cpp`，后者又依赖 `touchstone.cpp` 和一堆对话框控件……C++ 的头文件依赖会像滚雪球一样把半个工程拖进来。这个测试工程的选择是干脆利落的：**把整个 GUI 全部编进来**。好处是不用维护一份"精简源文件清单"，坏处是编译一次测试等于编译一次 GUI。

第二个问题是：**六个测试类怎么装进一个可执行文件？** Qt Test 的常规用法是一个测试类一个 `main`，这里采用"聚合器"模式：一个 `main.cpp` 依次 `qExec` 六个类。

#### 4.1.2 核心流程

测试程序从启动到退下的完整流程：

1. `main()` 创建 `QApplication`（注意：不是 `QCoreApplication`，原因见 4.1.3）。
2. 依次 `QTest::qExec(new XxxTests, argc, argv)` 执行六个测试类。
3. 每个类的所有 `private slots` 按声明顺序运行，断言失败被记录。
4. 各类的退出码用 `|=` 按位累积——**任何一类失败，总退出码就非零**，方便 CI 判断。
5. 进程退出码交给 `make check` 或调用方。

#### 4.1.3 源码精读

先看入口，总共只有 23 行：

[Software/PC_Application/LibreVNA-Test/main.cpp:L1-L23](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/main.cpp#L1-L23)

这段代码做了三件事：包含六个测试类的头文件；创建 `QApplication`；用 `status |=` 依次执行六个测试类并累积结果。注意第 12 行用的是 `QApplication` 而非 `QCoreApplication`——测试也要有完整的 widgets 环境，因为 `PortExtensionTests` 会调用 `PortExtension::edit()` 去创建真实的编辑对话框控件（见 4.2.3），没有 widgets 环境直接崩溃。这是"被测代码与 UI 耦合，测试就得把 UI 基础设施拉起来"的典型例子。

再看工程文件的关键几行：

[Software/PC_Application/LibreVNA-Test/LibreVNA-Test.pro:L1-L6](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/LibreVNA-Test.pro#L1-L6)

- `QT += testlib widgets network svg`：引入 Qt Test 框架和 GUI 所需模块。
- `CONFIG += qt console warn_on depend_includepath testcase`：其中 **`testcase`** 会让 qmake 额外生成 `make check` 目标和 `target_wrapper.sh` 包装脚本（后者被一并提交进了仓库，内容就是设置 `LD_LIBRARY_PATH`/`QT_PLUGIN_PATH` 后 `exec "$@"`）。
- `CONFIG -= app_bundle`：macOS 上不打包成 `.app`，保持命令行程序形态。

SOURCES 列表的开头和结尾最能说明"全量编译"策略：

[Software/PC_Application/LibreVNA-Test/LibreVNA-Test.pro:L8-L20](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/LibreVNA-Test.pro#L8-L20)

第 9 行是点睛之笔：**固件侧的 `Protocol.cpp` 也被编进 PC 测试**。这正是 u1-l3 讲过的"协议两端同源编译"策略在测试工程里的延续——测试环境和 GUI 用同一份协议实现。接下来第 10 行开始一直到第 163 行，是几乎完整的 GUI 源文件清单（校准、设备驱动、Traces、模式……），最后第 164-170 行才是六个测试文件自己。换句话说，**测试代码约 800 行，被拖进来的被测代码约 15 万行**。

依赖也原样照搬：

[Software/PC_Application/LibreVNA-Test/LibreVNA-Test.pro:L460-L468](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/LibreVNA-Test.pro#L460-L468)

`LIBS += -lusb-1.0`、C++17、git 哈希宏、固件版本号——与 GUI 工程完全一致。这份 `.pro` 实际上是 `LibreVNA-GUI.pro` 的超集（多了 testlib 和六个测试文件），维护时两边要同步增删文件。

#### 4.1.4 代码实践

**实践一：编译并运行测试工程**

1. **实践目标**：亲手跑通 LibreVNA-Test，拿到一份全量的通过/失败清单。
2. **操作步骤**（承接 u1-l3 的 GUI 构建环境，依赖完全相同：Qt6 + libusb）：
   ```bash
   cd Software/PC_Application/LibreVNA-Test
   qmake6 LibreVNA-Test.pro
   make -j$(nproc)
   ./LibreVNA-Test
   ```
   也可以用 `make check`（`testcase` 配置生成的目标），它会经由 `target_wrapper.sh` 运行测试。
3. **需要观察的现象**：终端按六个类依次输出 Qt Test 报告，形如 `********* Start testing of ParameterTests *********`，每个用例一行 `PASS` 或 `FAIL!`，每类结束有 `Totals: ... passed, ... failed`；最后返回 shell 时可用 `echo $?` 查看总退出码（0 = 全部通过）。
4. **预期结果**：六个类共 21 个测试函数（Util 5 + PortExtension 2 + Parameter 6 + fft 4 + ImpedanceRenormalization 1 + Calibration 3）全部 PASS，退出码为 0。若某个环境相关的用例失败（例如涉及平台路径），如实记录失败项与报错信息。
5. **待本地验证**：本讲义在编写时未实际执行编译运行，上述命令与输出格式请以本地结果为准。

**进阶操作**：`./LibreVNA-Test ParameterTests` 只跑一个类；`./LibreVNA-Test ParameterTests::S2ABCD` 只跑一个用例；`./LibreVNA-Test -functions` 列出全部可运行的测试函数名。这些是 Qt Test 内建的命令行约定，调试单个失败时非常顺手。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `main.cpp` 里用 `status |= QTest::qExec(...)` 而不是 `status = QTest::qExec(...)`？

**答案**：`qExec` 每次返回该类的失败计数（非零即有失败）。用 `|=` 按位累积，可以保证前面某个类失败后，后续类的结果不会把失败码"冲掉"；六个类里任何一个失败，进程退出码都非零。如果用 `=`，最后一个类通过就会掩盖前面的失败。

**练习 2**：如果不提交 `target_wrapper.sh` 到仓库，会有什么影响？

**答案**：`target_wrapper.sh` 本是 qmake 在生成 `make check` 目标时自动产出的构建产物（设置库和插件搜索路径后 exec 测试程序），按理应被 `.gitignore` 忽略。它被提交只是历史意外；删掉它不影响 `./LibreVNA-Test` 直接运行，只影响 `make check` 在缺少系统路径环境时的包装调用，下次运行 qmake 会重新生成。

### 4.2 校准与参数测试用例

#### 4.2.1 概念说明

这个模块逐个拆开六份测试文件，看它们各自用哪种套路对付哪种数学。先给一张总览表，后文挑最有教学价值的三个精读：

| 测试文件 | 被测对象 | 主要套路 | 期望值来源 |
|---|---|---|---|
| `parametertests.cpp` | `Sparam`/`ABCDparam`/`Zparam` 互转 | 已知答案 + 独立公式 | 硬编码高精度常数 / 测试内手写教科书公式 |
| `calibrationtests.cpp` | `Calibration::hasFrequencyOverlap` | 合成数据 + 行为断言 | 构造数据时自己选定的起止频率、点数、栅格类型 |
| `ffttests.cpp` | `Fft::transform` | 手算 DFT + 往返 | 解析计算 + 还原性 |
| `utiltests.cpp` | 圆拟合、版本比较、阻抗换算 | 解析构造 + 物理常识 + 边界 | 几何构造 / 手算 / `±inf` 边界 |
| `portextensiontests.cpp` | `PortExtension` 自动计算与修正 | 合成数据端到端 | 已知的时延/损耗参数（放进去多少，拿出来就该是多少） |
| `impedancerenormalizationtests.cpp` | `ImpedanceRenormalization` | 物理已知值 | 50Ω 负载归一到 75Ω 的反射系数 −0.2 |

#### 4.2.2 核心流程

**parametertests 的双套路**。`S2ABCD`/`ABCD2S` 是纯已知答案：输入一组"真实感"的 S 参数，期望的 A/B/C/D 四个复数以 16 位有效数字硬编码在测试里（作者显然在某个可信环境里算好后誊写进来的）。`S2Z_1P`/`S2Z_2P`/`Z2S_1P`/`Z2S_2P` 则是独立公式对照：测试内用文献里的标量公式现场算期望值，与被测的 Eigen 矩阵实现对质。

**calibrationtests 的栅格探测**。回忆 u9-l2：校准求解前要先用 `hasFrequencyOverlap` 检查各项测量的频率范围是否交叠、并投票决定按线性还是对数栅格取点。三个用例分别构造线性栅格、对数栅格、一者线性两者对义的混合栅格，各喂 1001 个点，断言探测出的起止频率、点数、栅格类型与构造时完全一致。

**ffttests 的一手一脚**。`fft` 用 5 点实序列手算 DFT；`fftAndIfft`/`ifftAndFft` 做往返。第四个 `fftAndIfftWithShift` 是**空函数**——一个测试盲区，4.2.5 会专门讨论。

#### 4.2.3 源码精读

**（a）已知答案测试的样板：S→ABCD**

[Software/PC_Application/LibreVNA-Test/parametertests.cpp:L12-L36](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/parametertests.cpp#L12-L36)

输入是四个接近"低损直通"的 S 参数（`S21 ≈ 0.9964 − 0.0254i`），期望的 ABCD 四元组精确到 17 位有效数字，然后对实部、虚部分别做 8 次 `qFuzzyCompare` 断言。它对应的被测实现是：

[Software/PC_Application/LibreVNA-GUI/Tools/parameters.cpp:L75-L86](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Tools/parameters.cpp#L75-L86)

这正是 S 参数转 ABCD 级联矩阵的标准公式（分母里的 \(2 S_{21}\sqrt{Z_{01} Z_{02}}\) 是归一化因子）。注意 `ABCD2S` 用例（L38-L63）把同一对数据反着喂——A/B/C/D 进、S 出，两个方向互相印证，已知答案与往返测试在这里合二为一。

**（b）独立公式对照与容差降级：S→Z**

[Software/PC_Application/LibreVNA-Test/parametertests.cpp:L85-L116](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/parametertests.cpp#L85-L116)

这个用例有两个值得驻足的细节：

1. **期望值来自测试内部的手写公式**：第 100-104 行现场展开了 \(\Delta_S = (1-S_{11})(1-S_{22}) - S_{12}S_{21}\) 与 \(Z_{11} = \frac{(1+S_{11})(1-S_{22}) + S_{12}S_{21}}{\Delta_S} Z_0\) 等教科书公式，与被测代码采用的矩阵形式
   \[ Z = \sqrt{z}\,(1+S)\,(1-S)^{-1}\,\sqrt{z} \]
   （见 [parameters.cpp:L186-L208](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Tools/parameters.cpp#L186-L208)）走的是**两条完全不同的算法路径**。矩阵求逆的舍入路径和手写公式的舍入路径天然不同，这正好引出第二个细节。
2. **容差主动降级**：第 106 行注释写明"浮点误差对 `qFuzzyCompare(double, double)` 太大，改用 `qFuzzyCompare(float, float)`"。相对容差从约 \(10^{-12}\) 放宽到约 \(10^{-5}\)。同时它还在 `Z0 = 10…100Ω` 的循环里重复验证，一条公式错一点都难逃十组断面。
3. 顺带注意 `S2Z_1P`（[L65-L83](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/parametertests.cpp#L65-L83)）里单端口公式 \(Z_{11} = \frac{1+S_{11}}{1-S_{11}} Z_0\) 同样是手写 oracle，连"1 端口 Sparam 与 2 端口 Sparam 走不同构造函数"这种重载分派也被顺带覆盖了。

**（c）校准栅格探测：合成数据的"种瓜得瓜"**

[Software/PC_Application/LibreVNA-Test/calibrationtests.cpp:L91-L136](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/calibrationtests.cpp#L91-L136)

`MixedDetection` 构造了 Open 用线性栅格、Short/Load 用对数栅格的"脏"数据（L112-L122），然后断言探测结果是"对数"。它的价值在于覆盖了**投票合并**逻辑：多个测量栅格类型不一致时以多数为准——这是纯单元测试很难想到构造、却真实会发生（用户中途改了扫描设置）的场景。被测函数声明在：

[Software/PC_Application/LibreVNA-GUI/Calibration/calibration.h:L144](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calibration.h#L144)

一个值得记录的**覆盖盲区**：calibrationtests 只测栅格探测，u9-l2 精读过的 `computeOSL`/`computeSOLT` 误差项求解与 `correctMeasurement` 修正矩阵——校准最核心的数学——**没有任何直接单元测试**（它们只被 u10-l3 提到的 Integrationtests 间接着色）。想给校准求解器补测试，是本讲方法的天然延伸方向。

**（d）FFT：手算期望值与"逆变换不缩放"契约**

[Software/PC_Application/LibreVNA-Test/ffttests.cpp:L9-L30](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/ffttests.cpp#L9-L30)

期望值是可以亲手验算的。对 \(x = \{1,2,3,4,5\}\) 做 5 点 DFT \(X[k] = \sum_n x[n]\,e^{-2\pi j kn/5}\)：

- \(X[0] = 1+2+3+4+5 = 15\)（直流就是求和）；
- \(X[1]\) 实部 \(= \sum_n x[n]\cos(72°n) = 1 + 0.618 - 2.427 - 3.236 + 1.545 = -2.5\)；
- \(X[1]\) 虚部 \(= -\sum_n x[n]\sin(72°n) = -(1.902 + 1.763 - 2.351 - 4.755) = +3.44095\ldots\)

与硬编码的 `complex(-2.5, 3.440954801177934)` 逐位吻合——这就是"期望值来自被测代码之外的独立来源"的含义。另外注意第 14 行的比较器用的是 **1e-14 绝对容差**而非 `qFuzzyCompare`：因为 FFT 输出量级跨度大，相对容差对小幅值 bin 过松、对大幅值 bin 过严，绝对容差在这里更贴切。容差选择没有银弹，**跟着数据的数值结构走**。

[Software/PC_Application/LibreVNA-Test/ffttests.cpp:L32-L54](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/ffttests.cpp#L32-L54)

往返用例里第 39 行和第 50 行都出现 `d /= data.size()`——这不是可有可无的归一化，而是在兑现被测接口的文档契约：

[Software/PC_Application/LibreVNA-GUI/Traces/fftcomplex.h:L35-L38](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/fftcomplex.h#L35-L38)

注释明说"逆变换不做缩放，因此不是真正的逆"。u8-l6 讲过这是 Nayiki 库的设计（正逆都不除 N），测试用例把这个易踩的坑固化成了可执行文档：忘了除以 N，往返立刻不还原。

**（e）端口延伸：合成数据端到端**

[Software/PC_Application/LibreVNA-Test/portextensiontests.cpp:L8-L25](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/portextensiontests.cpp#L8-L25)

构造函数在测试对象里预置了 501 点合成数据：端口 1 是理想开路 `S11 = 1.0`；端口 2 用 `Util::addTransmissionLine(0.5, 50.0, 1e-9, 10, f)` 造出"反射系数 0.5 的负载 + 1ns 单向时延 + 损耗 10 的传输线"的测量值（该工具按校准件偏移线模型实现，声明于 [util.h:L134](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Util/util.h#L134)）。**种进去 1ns，等会儿就该挖出来 1ns**——期望值来自合成参数，这是合成数据测试的精髓。

[Software/PC_Application/LibreVNA-Test/portextensiontests.cpp:L27-L54](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/portextensiontests.cpp#L27-L54)

`autocalc` 用例的流程是：`fromJSON` 选端口 → `edit()` 创建编辑对话框（不 `exec()`，只搭好控件和信号连接）→ `measurementCompleted(dummyData)` 触发 u9-l5 精读过的自动计算（相位斜率解时延、√f 模型最小二乘解损耗，见 [portextension.cpp:L186-L210](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/portextension.cpp#L186-L210)，计算结果写回 UI 控件、再经信号同步进 `ext` 结构）→ `toJSON` 读出结果断言：`delay ≈ 1e-9`、`DCloss ≈ -10·log10(0.5)`。第 35 行的 `edit()` 调用解释了整个工程为什么必须链接 widgets 并使用 `QApplication`。注意断言同样转成了 `float` 比较——回归求解的数值容差需求与矩阵求逆类似。

`correct` 用例（[L56-L70](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/portextensiontests.cpp#L56-L70)）接着做第二轮验证：用刚标定出的参数对每个点执行 `transformDatapoint` 修正，断言 S22 回到纯实数 1.0——端口延伸把"线 + 0.5 负载"整体视为"等效损耗的线 + 理想开路"，修掉线之后剩下的就是理想开路。**一个测试类里先标定、后修正，两段断言互相咬合**。

**（f）阻抗再归一化：物理常识当期望值**

[Software/PC_Application/LibreVNA-Test/impedancerenormalizationtests.cpp:L18-L43](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/impedancerenormalizationtests.cpp#L18-L43)

期望值第 39 行的 −0.2 不是算出来的，是**背出来的物理**：50Ω 匹配负载在 50Ω 系统里 Γ=0，重新归一到 75Ω 参考阻抗后

\[ \Gamma' = \frac{Z - Z_0'}{Z + Z_0'} = \frac{50 - 75}{50 + 75} = -0.2 \]

短路永远反射 −1（与参考阻抗无关），开路永远 +1。第 30 行用 `0.9999999999999999` 代替精确的 1.0 并注明原因：Γ=1 处 \(Z \to \infty\)，实数除法会得到 `inf`。`utiltests.cpp` 里则更进一步，专门把这条边界写成了断言：

[Software/PC_Application/LibreVNA-Test/utiltests.cpp:L102-L107](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/utiltests.cpp#L102-L107)

让 `SparamToImpedance(1.0)` 先产出 `inf` 阻抗，再 `ImpedanceToSparam` 变回去，断言仍回到 1.0——**边界值（0、±1、inf）是数值测试性价比最高的投入点**，因为连续区间中段通常表现良好，奇点才是翻车现场。`utiltests` 的其余用例同理：圆拟合用解析几何构造理想圆/弧并加噪声后放宽到 0.1 绝对容差（[L47-L65](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/utiltests.cpp#L47-L65)），版本比较用一张手写的真值表做精确布尔断言（[L67-L80](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/utiltests.cpp#L67-L80)）。

#### 4.2.4 代码实践

**实践二：三问法读一个测试**

1. **实践目标**：建立"任何测试都能用三问拆开"的阅读习惯。
2. **操作步骤**：任选 `NoisyCircleApproximation`（[utiltests.cpp:L47-L65](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/utiltests.cpp#L47-L65)），依次回答：
   - **输入从哪来？**（答：测试里 `polar(radius, angle)` 解析构造 + `srand(0)` 固定种子的伪噪声）
   - **期望值从哪来？**（答：构造时的圆心 `(2.34, 4.12)` 本身）
   - **容差为什么是这个数？**（答：噪声幅度 0.1，故用 `abs(差) <= 0.1` 的绝对容差，且 `srand(0)` 保证可复现）
3. **需要观察的现象**：你会发现三问答完，这个测试的设计意图、强度边界、可维护性全部浮现；同理可以拆开其余 20 个用例。
4. **预期结果**：三问法适用于本工程全部 21 个测试函数，无一例外。
5. 此实践为纯源码阅读，无需运行环境。

#### 4.2.5 小练习与答案

**练习 1**：`fftAndIfftWithShift` 是一个空函数（[ffttests.cpp:L56-L59](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/ffttests.cpp#L56-L59)），运行时它会怎样？这暴露了什么问题？

**答案**：Qt Test 对私有槽的运行不要求其中有断言，空槽会被正常执行并报告 `PASS`（0 个断言、0 次失败）。它因此是一个**虚假的绿色**：测试统计里贡献一个通过，实际什么都没验证。这暴露了"绿灯 ≠ 被覆盖"——评估测试强度要看断言密度和覆盖面，不能只看通过数。它多半是作者预留的 TODO（名字里预告了 FFT + fftshift 的往返场景），补全它是读者现成的练手机会。

**练习 2**：`S2Z_2P` 为什么把期望值写成测试内的标量公式，而不是像 `S2ABCD` 那样硬编码 17 位常数？

**答案**：硬编码常数的维护成本高（换一组输入就要重新誊写四个 17 位数），而且它是"一次性"的——只能防回归。测试内手写公式让用例可以循环扫过 `Z0 = 10…100Ω` 十组断面，期望值随输入自动生成，强度和维护性都更好。代价是期望公式若与被测实现抄自同一处，会"同源失明"——所以 `S2ABCD` 的硬编码值反而提供了独立于文献公式的第二个参照。两种套路并存是刻意为之。

**练习 3**：`impedancerenormalizationtests` 为什么给开路用 `0.9999999999999999` 而短路用精确的 `-1.0`？

**答案**：换域公式 \(Z = Z_0\frac{1+\Gamma}{1-\Gamma}\) 在 Γ = +1 处分母为零，会得到 `inf` 阻抗，后续矩阵运算把 `inf` 传得到处都是；而 Γ = −1 只让分子为零，得到有限的 0Ω，完全安全。用 `1-1e-16`（在 double 里恰好是距 1.0 最近的下一个数）绕开奇点，断言 `qFuzzyCompare(..., 1.0)` 依然成立。

### 4.3 新增用例方法

#### 4.3.1 概念说明

会读测试之后，本模块解决"写"：为 `parameters.h` 中**没有被任何测试覆盖**的 `Tparam`（散射传输参数，级联形式）补测试。先看覆盖现状——`parametertests.h` 声明的六个槽只覆盖三条路径：

```text
S ⇄ ABCD    （S2ABCD / ABCD2S）
S → Z       （S2Z_1P / S2Z_2P）
Z → S       （Z2S_1P / Z2S_2P）
─────────────────────────────────
S ⇄ T       ✗ 无测试        S ⇄ Y  ✗ 无测试
ABCD inverse/root  ✗        swapPorts / reduceTo  ✗
```

而 `Tparam` 在产品代码里并非死代码：u9-l5 讲过的 2x-Thru 去嵌入正是靠 T/ABCD 参数的级联与求逆来剥离夹具的。它无测试，正是"最值得补的第一个洞"。

#### 4.3.2 核心流程

给现有测试类新增一个用例只需要**两处改动、五步流程**（因为是向已有文件添加代码，连 `.pro` 都不用碰——`parametertests.cpp` 已在 SOURCES 里）：

1. **选函数**：从 `parameters.h` 里挑一个未覆盖、且你能独立算出期望值的函数（本文选 `Tparam(const Sparam&)`）。
2. **手算期望值**：找一组有物理意义、能手算的输入。理想直通（\(S_{11}=S_{22}=0,\ S_{12}=S_{21}=1\)）是最优选。
3. **写断言**：在 `parametertests.cpp` 实现测试函数，期望值与容差按 4.2 的套路选。
4. **登记**：在 `parametertests.h` 的 `private slots:` 里加一行声明。**忘记这一步，写了实现也不会被执行**——Qt Test 靠 moc 枚举私有槽发现用例，未声明的普通成员函数是隐身 的。
5. **运行验证**：重编译后用 `./LibreVNA-Test ParameterTests::新用例名` 单独运行，再跑全量确认无副作用。

#### 4.3.3 源码精读

被测的转换公式在（本讲的实践靶子）：

[Software/PC_Application/LibreVNA-GUI/Tools/parameters.cpp:L88-L98](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Tools/parameters.cpp#L88-L98)

即

\[ T = \begin{pmatrix} -\dfrac{S_{11}S_{22}-S_{12}S_{21}}{S_{21}} & \dfrac{S_{11}}{S_{21}} \\[2mm] -\dfrac{S_{22}}{S_{21}} & \dfrac{1}{S_{21}} \end{pmatrix} \]

把理想直通代入：四个分子中三个为 0，\(T_{11} = -(0-1)/1 = 1\)，\(T_{22} = 1/1 = 1\)，于是 **T = 单位矩阵**——物理上"直通就是什么都不做"，级联形式下正是恒等变换，这既是最好手算的期望值，也是对物理直觉的直接校验。再补一个非平凡断面防止"恰好全零"假通过：3dB 衰减器（\(S_{12}=S_{21}=1/\sqrt2\)）应得 \(T_{11} = \sqrt2/2\)、\(T_{22} = \sqrt2\)。

反向转换（`Sparam(const Tparam&)`）在 [parameters.cpp:L5-L11](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Tools/parameters.cpp#L5-L11)，两式互逆，正好构成往返测试的一对。

测试类的声明结构（登记位置）：

[Software/PC_Application/LibreVNA-Test/parametertests.h:L12-L19](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/parametertests.h#L12-L19)

#### 4.3.4 代码实践

**实践三：为 `Tparam` 补测试（本讲主实践）**

1. **实践目标**：把 4.3.2 的五步法完整走一遍，产出一个能防止 `Tparam` 公式回归的测试。

2. **操作步骤**：

   第一步，在 [parametertests.h:L18-L19](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/parametertests.h#L18-L19) 的 `private slots:` 末尾追加两行声明：

   ```cpp
   void S2T_2P();
   void T2S_roundtrip();
   ```

   第二步，在 `parametertests.cpp` 末尾追加实现（**以下为示例代码**，非项目原有内容；`Tparam` 头文件已随 `Tools/parameters.h` 一并包含）：

   ```cpp
   void ParameterTests::S2T_2P()
   {
       using namespace std::complex_literals;

       // 场景一：理想直通 S11=S22=0, S12=S21=1
       // 手算期望：T11=-(0-1)/1=1, T12=0/1=0, T21=-0/1=0, T22=1/1=1 → 单位矩阵
       auto S = Sparam(0.0, 1.0, 1.0, 0.0);
       auto t = Tparam(S);
       QVERIFY(qFuzzyCompare(t.get(1,1).real(), 1.0));
       QVERIFY(qFuzzyIsNull(t.get(1,1).imag()));
       QVERIFY(qFuzzyIsNull(t.get(1,2).real()));
       QVERIFY(qFuzzyIsNull(t.get(1,2).imag()));
       QVERIFY(qFuzzyIsNull(t.get(2,1).real()));
       QVERIFY(qFuzzyIsNull(t.get(2,1).imag()));
       QVERIFY(qFuzzyCompare(t.get(2,2).real(), 1.0));
       QVERIFY(qFuzzyIsNull(t.get(2,2).imag()));

       // 场景二：理想 3dB 衰减器 S12=S21=1/sqrt(2)
       // 手算期望：T11=0.5*sqrt(2)=sqrt(2)/2, T22=sqrt(2)
       auto atten = 1.0 / sqrt(2.0);
       auto S2 = Sparam(0.0, atten, atten, 0.0);
       auto t2 = Tparam(S2);
       QVERIFY(qFuzzyCompare(t2.get(1,1).real(), sqrt(2.0) / 2.0));
       QVERIFY(qFuzzyCompare(t2.get(2,2).real(), sqrt(2.0)));
       QVERIFY(qFuzzyIsNull(t2.get(1,2).real()));
       QVERIFY(qFuzzyIsNull(t2.get(2,1).real()));
   }

   void ParameterTests::T2S_roundtrip()
   {
       using namespace std::complex_literals;

       // 沿用本文件其他用例的"真实感"数据
       auto S = Sparam(0.0038 + 0.0248i, 0.9961 - 0.0250i,
                       0.9964 - 0.0254i, 0.0037 + 0.0249i);
       auto roundtrip = Sparam(Tparam(S));

       // 参照 S2Z_2P 的做法：往返经过两条求逆路径，降到 float 容差
       QVERIFY(qFuzzyCompare((float)roundtrip.get(1,1).real(), (float)S.get(1,1).real()));
       QVERIFY(qFuzzyCompare((float)roundtrip.get(1,1).imag(), (float)S.get(1,1).imag()));
       QVERIFY(qFuzzyCompare((float)roundtrip.get(1,2).real(), (float)S.get(1,2).real()));
       QVERIFY(qFuzzyCompare((float)roundtrip.get(1,2).imag(), (float)S.get(1,2).imag()));
       QVERIFY(qFuzzyCompare((float)roundtrip.get(2,1).real(), (float)S.get(2,1).real()));
       QVERIFY(qFuzzyCompare((float)roundtrip.get(2,1).imag(), (float)S.get(2,1).imag()));
       QVERIFY(qFuzzyCompare((float)roundtrip.get(2,2).real(), (float)S.get(2,2).real()));
       QVERIFY(qFuzzyCompare((float)roundtrip.get(2,2).imag(), (float)S.get(2,2).imag()));
   }
   ```

   断言风格说明：期望值精确为 0 的量用 `qFuzzyIsNull`（`qFuzzyCompare` 对接近 0 的相对比较不可靠，见 2.3），非零量用 `qFuzzyCompare(double)`——本转换是纯标量除法、无矩阵求逆，double 档容差足够；往返用例参照 `S2Z_2P` 的先例降为 float。

   第三步，重新编译并单独运行：

   ```bash
   make -j$(nproc)
   ./LibreVNA-Test ParameterTests::S2T_2P ParameterTests::T2S_roundtrip
   ./LibreVNA-Test        # 全量回归，确认没有影响其他用例
   ```

3. **需要观察的现象**：新用例输出 `PASS`；全量运行时 `ParameterTests` 的用例计数从 6 变为 8，`Totals` 相应增加 2 个 passed；`-functions` 列表中出现两个新函数名。

4. **预期结果**：两个新用例通过，其余 21 个原有用例不受影响（总退出码仍为 0）。若 `T2S_roundtrip` 偶发失败，优先怀疑容差档位而非公式。

5. **待本地验证**：以上代码未在本讲义编写环境中编译运行，登记与编译步骤请以本地实际输出为准。

**实践收尾（题目要求的风格统计）**：对照自己刚写的用例与现有用例，记录三组差异并写进笔记——① 期望值来源：现有用例多为"誊写的高精度常数"或"测试内手写公式"，你的第一个用例用的是"解析手算的物理理想元件"（直通/衰减器），第二个用往返规避期望值；② 容差档位：现有用例在涉及求逆/回归的地方一律降 float，纯标量处用 double，你的用例遵循了同一规律；③ 断言粒度：现有用例对每个复数拆实虚各一条 `QVERIFY`，共 8 条/用例，保持这个粒度能让失败时精确定位到"哪个矩阵元素的哪一半"。

#### 4.3.5 小练习与答案

**练习 1**：如果把新用例的声明忘在 `parametertests.h` 之外，只写了 `.cpp` 实现，会发生什么？

**答案**：编译和链接都能通过（成员函数有定义），但 Qt Test 不会运行它——`QTest::qExec` 通过 moc 元对象枚举的是**私有槽**，普通成员函数不在其列。于是测试总数不变、报告全绿，新用例实际上是"隐身"的。这就是 4.3.2 第 4 步强调登记的原因，也是"绿灯 ≠ 被覆盖"的又一实例。

**练习 2**：`Tparam` 转换在什么输入下会失效？测试需要为此做什么？

**答案**：公式以 \(S_{21}\) 为统一分母，\(S_{21} = 0\) 时除零（实际是无意义的 \(T \to \infty\)，传输参数对"完全隔离"的网络不存在有限表示）。测试不应主动构造该输入当正常断言；若要覆盖，合理做法是断言结果为 `inf`/`NaN` 并注释说明这是已知奇点（参照 `utiltests` 对 Γ=1 的 inf 边界的处理方式）。

**练习 3**：为什么选"理想直通"和"3dB 衰减器"做 `S2T_2P` 的两个断面，而不是只测其中一个？

**答案**：逐项代入就能看清每个断面的"抓错半径"。直通（\(S_{11}=S_{22}=0,\ S_{12}=S_{21}=1\)）下 \(T_{11}=-(0-1)/1=1\)：外层负号若丢失会得 \(-1\)，立刻被抓住；但 \(T_{22}=1/S_{21}=1/1=1\)，若错写成 \(S_{21}\) 本身也等于 1——**抓不住**。3dB 衰减器正是为这个洞存在的：\(T_{22}=1/(1/\sqrt2)=\sqrt2 \ne 1/\sqrt2\)，分母"求倒数"这类标度错误在它面前现形。但两个断面有个共同盲区——\(S_{11}\) 与 \(S_{22}\) 都是 0，若 \(T_{12}=S_{11}/S_{21}\) 被误写为 \(S_{22}/S_{21}\)，两种输入下都恒等于 0，永远通过。**要咬住这类对称性错误，必须用 \(S_{11}, S_{22} \ne 0\) 且互不相等的一般数据**——`T2S_roundtrip` 恰好提供了（\(0.0038+0.0248j\) 与 \(0.0037+0.0249j\)）。三个用例各守一层：直通锁定物理直觉（级联恒等变换 = 单位阵），衰减器锁定标度，一般数据往返咬住公式细节。

## 5. 综合实践

**任务：给参数转换矩阵做一次"覆盖体检"，并补上第一个洞。**

把本讲三块知识串起来，完成一份可归档的测试笔记：

1. **跑基线**：按实践一编译运行 LibreVNA-Test，把 21 个（加上你新增的共 23 个）用例的通过/失败情况记入表格，注明版本 commit。
2. **画覆盖地图**：对照 [parameters.h:L38-L169](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Tools/parameters.h#L38-L169) 的全部公开接口，逐个标注"已测 / 未测 / 不可单测"，并给未测项排优先级（提示：`Sparam::swapPorts` 与 `Sparam::reduceTo` 是纯排列操作、期望值最易手算；`ABCDparam::root` 涉及矩阵开方，可先用"root 的平方应还原原矩阵"这一往返性质构造断言；`Yparam` 链路可仿照 `S2Z` 用手写公式对照）。
3. **补一个洞**：完成实践三的 `Tparam` 用例；学有余力再从第 2 步的清单里挑一个实现。
4. **写结论**：用三问法（输入来源 / 期望来源 / 容差理由）为你新增的每个用例写一行注释，并统计"每用例断言数"与现有代码（平均约 8 条）的差异。

预期成果：一份覆盖地图 + 至少两个合入 `parametertests` 的新用例 + 一页体检报告。这份地图同时是下一讲（u11-l2 精读 `Tools/` 数学库）的预习提纲。

## 6. 本讲小结

- LibreVNA-Test 采用**聚合器入口**：一个 `main.cpp` 用 `QTest::qExec` 串联六个测试类，`status |=` 累积退出码；`.pro` 把**整个 GUI 加固件协议层**全部编进测试，`CONFIG += testcase` 提供 `make check`。
- 测试类 = `QObject` 子类 + `private slots`，**声明即登记**：漏写槽声明，实现再多也是隐身的；空槽会"虚假地 PASS"（`fftAndIfftWithShift` 即是明证）。
- 数值测试三板斧：**已知答案**（硬编码或解析手算，如理想直通的 T 参数是单位矩阵）、**独立公式对照**（`S2Z_2P` 用手写教科书公式对质 Eigen 矩阵实现）、**往返一致性**（FFT↔IFFT、S⇄ABCD），强度递减、成本递减。
- 期望值必须来自**被测代码之外**：物理常识（50Ω 负载归一到 75Ω 得 −0.2）、合成参数（种进 1ns 时延就要挖出 1ns）、解析几何（理想圆的圆心），唯独不能用被测函数自己算。
- 容差是设计决策：相对容差（`qFuzzyCompare`）在求逆/回归等双路径场景要降级为 float（约 \(10^{-5}\)），量级跨度大的 FFT 用绝对容差（1e-14），期望值为零用 `qFuzzyIsNull`；边界值（0、±1、inf）是数值测试性价比最高的投入点。
- 覆盖现状有明确盲区：`Tparam`/`Yparam`/`ABCDparam::root`/`swapPorts`/`reduceTo` 全部无测试，校准误差项求解器也只有间接覆盖——补第一个洞只需改头文件与实现两处，`.pro` 都不必动。

## 7. 下一步学习建议

- **下一讲 u11-l2（工具箱：S 参数数学、E 系列、阻抗匹配与自定义控件）** 将正面精读 `Tools/parameters.cpp` 的完整数学体系与 Eigen 用法，本讲的覆盖地图正好作为预习提纲——先知道哪些函数没被测试保护，再读实现时会对风险点格外敏感。
- 想继续补测试的读者，优先顺序建议：`Sparam::swapPorts`/`reduceTo`（纯排列，10 分钟一个用例）→ `Yparam` 链路（仿 `S2Z` 手写公式）→ `ABCDparam::root`（用"平方还原"性质）→ 校准求解器 `computeOSL`（用理想校准件 + 理想误差模型构造可手算的闭环，难度最高）。
- 顺手填掉 `fftAndIfftWithShift` 这个空壳：仿照 `fftAndIfft` 的写法，在两次变换之间插入/移除 fftshift 的等价操作（交换前后半段），断言还原。
- 若想了解黑盒端到端测试与单元测试如何互补，回看 u10-l3 讲过的 `Software/Integrationtests/`：它用 SCPI 拉起真实（无头）GUI 验证整机行为，与本讲的数值白盒测试分守两道防线。
